"""llms.txt names all three ways through a gate, and reads every price.

MEASURED ON THE LIVE FILE, 2026-09-17 (https://dchub.cloud/llms.txt, 23,736 B):

    claim_free_key   3      $10       0      $99   0
    "Pro"            0      /go/c     0      /upgrade/h   0

The full agent brief — the surface most likely to be ingested WHOLE by a model
— named the free rung three times and carried NO paid rung at all, nor any
mention that a gated result hands back a tokenized link to relay. The only
price in the file was Developer $49/mo, from the paid-API heading. The same
day, /AGENTS.md, /api/v1/ai-agents.json, /pricing and every gated tool envelope
all led with $10 → Pro $99.

WHAT THESE PIN
  * all three rungs are present, in the order a caller can take them;
  * every figure is READ from the module that owns it, never typed here or
    there — the $49 heading this sits beside was wrong for six months exactly
    because it was a literal;
  * an unreadable figure DROPS its rung instead of guessing, and the block
    degrades to empty rather than to a wrong price;
  * the relay instruction rides with it, since a link relayed to a generic
    pricing page loses the session binding.
"""
import ast
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SRC = (ROOT / "ai_discovery_routes.py").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def mod():
    import ai_discovery_routes
    return ai_discovery_routes


@pytest.fixture(scope="module")
def ladder(mod):
    return mod._llms_unlock_ladder()


def test_all_three_rungs_are_named(ladder):
    assert "claim_free_key" in ladder
    assert "one-time" in ladder
    assert "/mo" in ladder


def test_the_rungs_are_in_the_order_a_caller_can_take_them(ladder):
    """★ Free first. An anonymous caller asked to find a human with a card
    before being told about the key it can mint itself is the same defect the
    gate copy carried until the same day."""
    free = ladder.index("claim_free_key")
    pack = ladder.index("one-time")
    sub = ladder.index("/mo")
    assert free < pack < sub
    # P0-B (2026-09-21): the agent rungs — pack, then Developer — come before
    # Pro, the human screener's plan.
    assert pack < ladder.index("**Developer $") < ladder.index("**Pro $")


def test_every_price_is_read_from_the_module_that_owns_it(ladder, mod):
    """★ NOT a literal. The $49 heading beside this one was a literal and was
    wrong for six months."""
    from routes.mcp_conversion_plays import PACK10_CREDITS, PACK10_PRICE_CENTS
    import tier_registry as tr

    assert "$%d one-time" % (PACK10_PRICE_CENTS // 100) in ladder
    assert format(PACK10_CREDITS, ",") in ladder
    assert "$%d/mo" % int(tr.price("pro")) in ladder
    assert "$%d/mo" % int(tr.price("developer")) in ladder
    for tier in ("pro", "developer"):
        per_day = tr.calls_per_day(tier)
        if per_day:
            # Lane-named: Developer's REST and MCP quotas differ.
            assert "%s MCP calls/day" % format(int(per_day), ",") in ladder

    # And the CODE carries no hardcoded money. Scanned with the docstring
    # removed: that docstring quotes the measured $10/$99/$49 on purpose —
    # a comment explaining a price drift has to be able to name the prices,
    # and a guard that reads prose as code is the same defect one level up.
    fn = next(n for n in ast.walk(ast.parse(SRC))
              if isinstance(n, ast.FunctionDef) and n.name == "_llms_unlock_ladder")
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)
            and isinstance(fn.body[0].value.value, str)):
        code_nodes = fn.body[1:]
    else:  # pragma: no cover - the function is documented
        code_nodes = fn.body
    assert code_nodes, "the ladder function is only a docstring"
    code = "\n".join(ast.get_source_segment(SRC, n) or "" for n in code_nodes)
    assert "claim_free_key" in code, (
        "the scanned segment is not the function body — the assertions below "
        "would pass over an empty string")
    for literal in ("$10", "$99", "$49", "1,000 API calls", "2,000 ", "500 "):
        assert literal not in code, (
            "%r is typed into the ladder — it must be read from the registry"
            % literal)


def test_an_unreadable_price_drops_its_rung_rather_than_guessing(mod, monkeypatch):
    """★ A missing number is visible where a wrong one is not — the same
    contract _llms_paid_heading and canon_text already keep."""
    monkeypatch.setitem(sys.modules, "routes.mcp_conversion_plays", None)
    out = mod._llms_unlock_ladder()
    assert "one-time" not in out, "the pack rung survived an unreadable price"
    assert "claim_free_key" in out, "the readable rungs went with it"
    assert "/mo" in out


def test_with_nothing_readable_the_block_is_empty(mod, monkeypatch):
    """No paid rung means no heading either — llms.txt says less, never wrong."""
    monkeypatch.setitem(sys.modules, "routes.mcp_conversion_plays", None)
    monkeypatch.setitem(sys.modules, "tier_registry", None)
    assert mod._llms_unlock_ladder() == ""


def test_the_relay_instruction_rides_with_the_ladder(ladder):
    """A link relayed as 'go to the pricing page' loses the session binding
    that would have unlocked the very next call."""
    assert "verbatim" in ladder.lower()
    assert "MINTED FOR YOUR SESSION" in ladder


def test_the_ladder_is_actually_served(mod):
    """★ A builder nothing calls publishes nothing. Pinned against the
    serve_llms_txt body, not against a live fetch."""
    fn = next(n for n in ast.walk(ast.parse(SRC))
              if isinstance(n, ast.FunctionDef) and n.name == "serve_llms_txt")
    body = ast.get_source_segment(SRC, fn) or ""
    assert "_llms_unlock_ladder()" in body
    # Above the paid-API heading, so the cheapest way through is read first.
    assert body.index("_llms_unlock_ladder()") < body.index("_llms_paid_heading()")


def test_developer_is_on_the_ladder_even_when_the_pack_is_unreadable(mod, monkeypatch):
    """An unreadable pack drops the pack line, not the agent rung."""
    monkeypatch.setitem(sys.modules, "routes.mcp_conversion_plays", None)
    out = mod._llms_unlock_ladder()
    assert "**Developer $" in out
    assert out.index("**Developer $") < out.index("**Pro $")


def test_llms_full_carries_the_ladder_it_promises(mod):
    """llms-full.txt told agents pricing is "in the unlock ladder further down
    this file" and carried no ladder. It also claimed Developer gets "1,000
    requests/day vs 100 free" — free is 10 on every lane."""
    import flask
    app = flask.Flask(__name__)
    mod.register_discovery_routes(app)
    body = app.test_client().get("/llms-full.txt").get_data(as_text=True)
    assert "these are the three ways through" in body
    assert "**Developer $" in body
    assert "vs 100 free" not in body
    import tier_registry as tr
    assert "Developer Tier ($%d/month)" % int(tr.price("developer")) in body
    assert "%s MCP calls/day" % format(int(tr.calls_per_day("developer")), ",") in body
