"""REST free walls hand a measured /go/c pack checkout and the agent ladder.

P0-C (2026-09-21). Measured that morning: keyless GET /api/v1/facilities
answered `upgrade_url: https://dchub.cloud/pricing`. That link is unmeasured and
bound to nothing, and it went to the one page a slow asset read had just
replaced with a cached "briefly unavailable" stub. The wall now names the $10
pack as a signed /go/c checkout, with the ladder beside it in the order agents
buy it.
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


def test_upgrade_url_is_the_pack_checkout_not_the_pricing_page(signed):
    assert _go(cct.rest_wall_ladder()["upgrade_url"]) == ("metered", "", "")


def test_ladder_is_pack_then_developer_then_pro_with_read_prices(signed):
    import tier_registry as tr

    opts = cct.rest_wall_ladder()["upgrade_options"]
    assert [o["plan"] for o in opts] == ["pack", "developer", "pro"]
    assert [_go(o["url"])[0] for o in opts] == ["metered", "developer", "pro"]
    assert tr.price_display("developer") in opts[1]["label"]
    assert tr.price_display("pro") in opts[2]["label"]
    assert "one-time" in opts[0]["label"]


def test_caller_independent_so_safe_inside_a_shared_cache(signed):
    """The deals payload is memoized and served to every caller, so nothing in
    it may be bound to one caller's key or session."""
    a, b = cct.rest_wall_ladder(), cct.rest_wall_ladder()
    assert a == b
    for o in a["upgrade_options"]:
        _, ref, sid = _go(o["url"])
        assert ref == "" and sid == ""


def test_without_a_signing_secret_it_falls_back_to_the_pricing_page(monkeypatch):
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    wall = cct.rest_wall_ladder()
    assert wall["upgrade_url"] == cct._PRICING_URL
    assert all(o["url"] == cct._PRICING_URL for o in wall.get("upgrade_options", []))


def test_the_deals_free_wall_renders_the_ladder(signed, monkeypatch):
    """Rendered, not grepped: the seed path runs with no database."""
    import flask
    from routes import deals_routes as dr

    monkeypatch.delenv("DATABASE_URL", raising=False)
    dr._FREE_TX_CACHE.clear() if hasattr(dr._FREE_TX_CACHE, "clear") else None
    app = flask.Flask(__name__)
    with app.app_context():
        body = dr._get_transactions_free().get_json()
    assert body["tier"] == "free"
    assert _go(body["upgrade_url"])[0] == "metered"
    assert [o["plan"] for o in body["upgrade_options"]] == ["pack", "developer", "pro"]


def test_the_facilities_free_payload_spreads_the_ladder_and_types_no_url():
    """main.py is too heavy to import in a unit test, so read the one
    `_free_payload` dict: it must spread `_wall` and carry no literal
    upgrade_url key."""
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    hits = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "_free_payload" for t in n.targets)]
    assert len(hits) == 1, "expected exactly one _free_payload assignment"
    d = hits[0].value
    assert isinstance(d, ast.Dict)
    keys = [k.value for k in d.keys if isinstance(k, ast.Constant)]
    assert "tier" in keys, "scanned the wrong dict"
    assert "upgrade_url" not in keys
    spreads = [v for k, v in zip(d.keys, d.values) if k is None]
    assert any(isinstance(v, ast.Name) and v.id == "_wall" for v in spreads)
