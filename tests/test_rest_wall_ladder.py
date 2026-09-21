"""REST free walls offer only what actually opens them.

P0-C (2026-09-21). be#5072 replaced `upgrade_url: https://dchub.cloud/pricing`
on the keyless facilities and free-transactions walls with a measured /go/c
checkout, but led with the $10 pack and listed pack → Developer → Pro. Measured
after it shipped: both REST lists sit behind require_plan('pro'), so neither
the pack nor Developer opens them over REST, and a Developer key is refused
outright. The wall now leads with the plan that opens the endpoint and lists the
pack and Developer as what they are: full results through an MCP tool.

Later the same day (frontend#1534) the facilities list itself started taking the
pack and Developer over REST (util/rest_pack_access.py). Its wall now uses the
pack-led ladder, and the test below reads which ladder from the route's own
gate rather than from a literal.
"""
import ast
import pathlib

import pytest

from routes import checkout_click_tracker as cct

SECRET = "test-internal-key-not-a-real-secret"
ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def signed(monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", SECRET)


def _go(url):
    """Verify a /go/c URL the way the redirect handler does; return its fields."""
    assert url.startswith(cct._GO_BASE), url
    plan, ref, sid, ok = cct._verify(url[len(cct._GO_BASE):])
    assert ok, url
    return plan, ref, sid


def test_upgrade_url_is_the_checkout_of_the_plan_that_opens_the_endpoint(signed):
    wall = cct.rest_wall_ladder(opens_on_rest="pro", mcp_tool="search_facilities")
    assert _go(wall["upgrade_url"]) == ("pro", "", "")


def test_only_the_opening_plan_is_labelled_as_opening_this_endpoint(signed):
    """The regression this file exists for: a rung shown as the way through a
    REST wall must be a rung that REST admits."""
    opts = cct.rest_wall_ladder(opens_on_rest="pro", mcp_tool="search_facilities")["upgrade_options"]
    rest = [o for o in opts if o["opens"] == "rest"]
    assert [o["plan"] for o in rest] == ["pro"]
    assert opts[0]["opens"] == "rest", "the way through this endpoint leads"
    assert "opens this endpoint" in opts[0]["label"]
    for o in opts:
        if o["opens"] != "rest":
            assert "opens this endpoint" not in o["label"], o


def test_pack_and_developer_are_named_as_mcp_routes_to_the_full_result(signed):
    import tier_registry as tr

    opts = cct.rest_wall_ladder(opens_on_rest="pro", mcp_tool="search_facilities")["upgrade_options"]
    assert [o["plan"] for o in opts] == ["pro", "pack", "developer"]
    assert [_go(o["url"])[0] for o in opts] == ["pro", "metered", "developer"]
    for o in opts[1:]:
        assert o["opens"] == "mcp" and o["mcp_tool"] == "search_facilities"
        assert "`search_facilities`" in o["label"] and "not this REST endpoint" in o["label"]
    assert tr.price_display("pro") in opts[0]["label"]
    assert tr.price_display("developer") in opts[2]["label"]
    assert "one-time" in opts[1]["label"]


def test_without_an_mcp_tool_only_the_opening_plan_is_offered(signed):
    opts = cct.rest_wall_ladder(opens_on_rest="pro")["upgrade_options"]
    assert [o["plan"] for o in opts] == ["pro"]


def test_caller_independent_so_safe_inside_a_shared_cache(signed):
    """The deals payload is memoized and served to every caller, so nothing in
    it may be bound to one caller's key or session."""
    a = cct.rest_wall_ladder(opens_on_rest="pro", mcp_tool="list_transactions")
    assert a == cct.rest_wall_ladder(opens_on_rest="pro", mcp_tool="list_transactions")
    for o in a["upgrade_options"]:
        _, ref, sid = _go(o["url"])
        assert ref == "" and sid == ""


def test_without_a_signing_secret_it_falls_back_to_the_pricing_page(monkeypatch):
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    wall = cct.rest_wall_ladder(opens_on_rest="pro", mcp_tool="search_facilities")
    assert wall["upgrade_url"] == cct._PRICING_URL
    assert all(o["url"] == cct._PRICING_URL for o in wall.get("upgrade_options", []))


def test_the_deals_free_wall_renders_the_honest_ladder(signed, monkeypatch):
    """Rendered, not grepped: the seed path runs with no database."""
    import flask
    from routes import deals_routes as dr

    monkeypatch.delenv("DATABASE_URL", raising=False)
    app = flask.Flask(__name__)
    with app.app_context():
        body = dr._get_transactions_free().get_json()
    assert body["tier"] == "free"
    assert _go(body["upgrade_url"])[0] == "pro"
    opts = body["upgrade_options"]
    assert [(o["plan"], o["opens"]) for o in opts] == [("pro", "rest"), ("pack", "mcp"), ("developer", "mcp")]
    assert all(o.get("mcp_tool") == "list_transactions" for o in opts[1:])


def _cheapest_rest_opener_of_list_facilities(tree):
    """Read from list_facilities itself: the plan its require_plan() admits, or
    'pack' when a key below that plan is sent through serve_below_plan."""
    fns = [n for n in tree.body
           if isinstance(n, ast.FunctionDef) and n.name == "list_facilities"]
    assert len(fns) == 1
    plans = [n.args[0].value for n in ast.walk(fns[0])
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_real_require_plan"
             and n.args and isinstance(n.args[0], ast.Constant)]
    assert len(plans) == 1, plans
    through_pack = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                       and n.func.id == "serve_below_plan" for n in ast.walk(fns[0]))
    return "pack" if through_pack else plans[0]


def test_the_facilities_free_payload_spreads_the_honest_ladder():
    """main.py is too heavy to import in a unit test, so read it: the one
    `_free_payload` dict spreads `_wall`, carries no literal upgrade_url, and
    `_wall` comes from rest_wall_ladder() in the mode list_facilities' own gate
    implies: the cheapest thing that opens the list over REST."""
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    hits = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "_free_payload" for t in n.targets)]
    assert len(hits) == 1, "expected exactly one _free_payload assignment"
    d = hits[0].value
    assert isinstance(d, ast.Dict)
    keys = [k.value for k in d.keys if isinstance(k, ast.Constant)]
    assert "tier" in keys, "scanned the wrong dict"
    assert "upgrade_url" not in keys
    assert any(k is None and isinstance(v, ast.Name) and v.id == "_wall"
               for k, v in zip(d.keys, d.values))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_rest_wall_ladder"]
    assert len(calls) == 1
    kw = {k.arg: k.value.value for k in calls[0].keywords if isinstance(k.value, ast.Constant)}
    opener = _cheapest_rest_opener_of_list_facilities(tree)
    assert opener == "pack", "list_facilities no longer takes the pack: re-read frontend#1534"
    assert kw == {"opens_on_rest": opener}


# ── the pack-led ladder: for a REST list the pack itself opens ──────────────

def test_the_pack_led_ladder_leads_with_the_pack_checkout(signed):
    wall = cct.rest_wall_ladder(opens_on_rest="pack")
    assert _go(wall["upgrade_url"]) == ("metered", "", "")


def test_the_pack_led_ladder_offers_pack_then_developer_both_opening_rest(signed):
    import tier_registry as tr
    from routes.mcp_conversion_plays import PACK10_CREDITS, PACK10_PRICE_CENTS

    opts = cct.rest_wall_ladder(opens_on_rest="pack", mcp_tool="search_facilities")["upgrade_options"]
    assert [(o["plan"], o["opens"]) for o in opts] == [("pack", "rest"), ("developer", "rest")]
    assert [_go(o["url"])[0] for o in opts] == ["metered", "developer"]
    assert "$%d one-time" % (PACK10_PRICE_CENTS // 100) in opts[0]["label"]
    assert format(PACK10_CREDITS, ",") in opts[0]["label"]
    assert tr.price_display("developer") in opts[1]["label"]
    assert all("mcp_tool" not in o and "not this REST endpoint" not in o["label"] for o in opts)
