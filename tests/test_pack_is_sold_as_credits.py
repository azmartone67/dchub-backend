"""The $10 pack is sold as CREDITS on every backend surface (owner, 2026-09-21).

Pricing-page accuracy audit (azmartone67/dchub-frontend#1550, #1534):
- The MCP server's CREDIT_HEAVY charges 13 heavy analysis tools 5 credits
  each, so "1,000 API calls" was false for them. The owner kept the 5-credit
  tools and chose to say credits everywhere.
- Separately, four backend walls told callers the Developer plan gives
  "1,000 calls/day". That is the REST rate_limit header value, which nothing
  enforces per day. /pricing and tier_registry.calls_per_day sell 500.

The surfaces are rendered where they can be. The source floor reads string
literals through the AST, so a comment that quotes the old phrase can neither
satisfy it nor trip it.
"""
import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SECRET = "test-internal-key-not-a-real-secret"

# The live modules that describe the pack or the Developer allowance to callers.
LIVE_MODULES = [
    "ai_surface_canon.py", "ai_discovery_routes.py", "mcp_gatekeeper.py", "media_cta.py",
    "routes/checkout_click_tracker.py", "routes/mcp_connect.py",
    "routes/mcp_conversion_plays.py", "routes/brain_capability_ledger.py",
    "routes/_stripe_links.py", "main.py",
]
PACK_AS_CALLS = re.compile(r"(?:1,000|%s|\{[^{}]+\}) API calls\b|1,000 calls\b")
DEV_THOUSAND = re.compile(r"1,000 calls/day")


def _string_literals(path):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def test_the_canonical_pack_offer_says_credits():
    import ai_surface_canon as canon
    from routes.mcp_conversion_plays import PACK10_CREDITS, PACK10_PRICE_CENTS
    assert canon._pack_offer() == "$%d one-time = %s API credits" % (
        int(PACK10_PRICE_CENTS) // 100, format(int(PACK10_CREDITS), ","))


def test_the_llms_ladder_sells_credits_and_states_the_heavy_rule():
    import ai_discovery_routes as routes
    text = routes._llms_unlock_ladder()
    assert "API credits" in text, text[:400]
    assert not PACK_AS_CALLS.search(text), text[:400]
    assert "1 per paid-tool call, 5 for the heavy" in text


def test_both_rest_pack_labels_say_credits(monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", SECRET)
    from routes import checkout_click_tracker as cct
    walls = (cct.rest_wall_ladder(opens_on_rest="pack"),
             cct.rest_wall_ladder(opens_on_rest="pro", mcp_tool="search_facilities"))
    labels = [o["label"] for w in walls for o in w["upgrade_options"] if o["plan"] == "pack"]
    assert len(labels) == 2, labels
    for label in labels:
        assert "API credits" in label and not PACK_AS_CALLS.search(label), label


@pytest.mark.parametrize("path", LIVE_MODULES)
def test_no_live_string_sells_the_pack_as_calls(path):
    hits = [(n, v[:90]) for n, v in _string_literals(path) if PACK_AS_CALLS.search(v)]
    assert hits == [], hits


def _main_helper(name):
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(fns) == 1, name
    ns = {}
    exec(compile(ast.Module(body=fns, type_ignores=[]), "main.py", "exec"), ns)
    return ns[name]


def test_developer_daily_quota_is_read_from_the_canon():
    import tier_registry
    assert _main_helper("_dev_daily_phrase")() == (
        f"{tier_registry.calls_per_day('developer'):,} MCP calls/day")
    hits = [(n, v[:90]) for n, v in _string_literals("main.py") if DEV_THOUSAND.search(v)]
    assert hits == [], hits
