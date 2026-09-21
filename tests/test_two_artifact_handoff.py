"""Two-artifact handoff + the free-key rung (2026-08-03).

The funnel's biggest leak, measured: 524 paywall hits -> 405 high-intent ->
404 relay minted -> 0 humans acted -> 0 paid. Every artifact in a paywall
envelope was AGENT-redeemable, and the gateway consumes the single-use claim
token in ~0.85s, so a human clicking later gets 410 Gone.

routes/human_relay.py shipped the human PAGE on 2026-07-27 and has been live
since — but nothing ever called make_relay_token(). Its docstring delegates
the mint to "the mcp-server's for_your_human builder", and server.mjs has no
such builder. The bridge was documented, routed and measured, never built.
"""
import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*p) -> str:
    with open(os.path.join(ROOT, *p), encoding="utf-8") as fh:
        return fh.read()


def _paywall_tree():
    return ast.parse(_src("utils", "paywall_response.py"))


def _for_your_human_literal():
    """The dict actually assigned to base['for_your_human'], as source text.

    Read from the AST, not a character window around the name. A ±2000-char
    window also swallows the COMMENTS around the field — and on 2026-09-20 an
    assertion here passed because "redeeming one cannot consume the other" in a
    nearby comment contained the phrase it was looking for, while the shipped
    field had been reworded. A comment about the guarantee is not the guarantee.
    """
    for node in ast.walk(_paywall_tree()):
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if (isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                    and t.slice.value == "for_your_human"):
                # The dict holds variables too (_human_url, tool_name), so it is
                # not literal_eval-able. Collect the STRING CONSTANTS — that is
                # the prose we ship, which is what this asserts on.
                return [n.value for n in ast.walk(node.value)
                        if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    raise AssertionError("base['for_your_human'] is no longer assigned here")


# ── 1. the human artifact is actually minted ──────────────────────────

def test_paywall_mints_a_human_relay_token():
    """THE pin. Without a caller, /upgrade/h/<token> is an unreachable page."""
    src = _src("utils", "paywall_response.py")
    assert "make_relay_token" in src, (
        "paywall envelope does not mint a human relay token — the human page "
        "is unreachable and the 404->0 leak stays open")
    assert "for_your_human" in src


def test_human_artifact_is_independent_of_the_agent_claim():
    """One token cannot serve both. If the human URL were derived from the
    claim token, redeeming would consume it and we are back to 410 Gone."""
    src = _src("utils", "paywall_response.py")
    i = src.index("for_your_human")
    # 2026-09-20: was a phrase match over a ±2000-char window. It pinned one
    # SPELLING, and the window included comments — so it stayed green when the
    # shipped wording changed, satisfied by "cannot consume the other" in a
    # comment nearby. Read the value we actually send.
    why = " ".join(_for_your_human_literal()).lower()
    assert re.search(r"not\s+consume|does\s+not\s+consume", why), (
        "the for_your_human field no longer tells the reader that redeeming "
        "the agent's claim does not consume this link")
    # the relay mint must not be fed the claim/pair code
    m = re.search(r"_mk_relay\(([^)]*)\)", src)
    assert m, "relay mint call not found"
    args = m.group(1)
    for forbidden in ("_pair_code", "claim", "redeem"):
        assert forbidden not in args, (
            f"relay token derived from {forbidden!r} — it would be consumed "
            f"by agent redemption")


def test_relay_url_is_relayed_in_prose_not_only_structured():
    """A client that drops structuredContent must still carry the sentence."""
    src = _src("utils", "paywall_response.py")
    # 2026-09-20: was `"FOR YOUR HUMAN" in src` — a banner string, not the
    # guarantee. The banner was reworded (it read as an instruction to the
    # model); what has to hold is that the relay URL reaches human_message at
    # all, which is what a structuredContent-dropping client relays.
    i = src.index("_human_url = ")
    tail = src[i:]
    assert "human_message" in tail, "the relay URL never reaches the prose"
    assert re.search(r"human_message'\]\s*\+=.{0,400}_human_url", tail,
                     re.S), (
        "human_message is mentioned after the mint but the relay URL is not "
        "appended to it")


def test_paywall_never_500s_when_the_relay_fails():
    """A paywall that raises because a link could not be built is worse than
    one with no link."""
    src = _src("utils", "paywall_response.py")
    i = src.index("from routes.human_relay import make_relay_token")
    # 2026-09-20: was src[i:i + 2200]. A FIXED SLICE measures length, not
    # content: adding a comment inside the try block pushed `except Exception:`
    # past character 2200 and the test went red while the fail-soft was intact
    # and untouched. Bound it by STRUCTURE — everything up to the next
    # top-level def — so the guarantee is what is measured.
    # 2026-09-20: was `"except Exception:" in tail and "pass" in tail` over a
    # fixed 2200-char slice. Two faults. The slice measured LENGTH — a comment
    # added inside the try pushed the except past it and went red while the
    # fail-soft was untouched. And `"pass" in tail` is satisfied by ANY pass in
    # the window, so replacing the handler body with `raise` kept it green.
    # Find the try that mints the relay and assert on its HANDLER.
    _mint = None
    for node in ast.walk(_paywall_tree()):
        if isinstance(node, ast.Try) and "make_relay_token" in ast.dump(node):
            _mint = node
            break
    assert _mint is not None, "the relay mint is no longer inside a try"
    assert _mint.handlers, "the relay mint's try has no except"
    for h in _mint.handlers:
        assert all(isinstance(st, ast.Pass) for st in h.body), (
            "the relay mint's except does something other than swallow — a "
            "paywall that raises because a link could not be built is worse "
            "than one with no link")
    tail = ""
    assert True, (
        "the relay mint is no longer wrapped in a fail-soft except/pass; a "
        "paywall that 500s because a link could not be built is worse than "
        "one with no link")


# ── 2. the free key is worth claiming ─────────────────────────────────

def test_free_key_beats_anonymous_on_the_mcp_surface():
    """They were BOTH 10/day, so claiming a key bought an agent nothing on
    the surface agents use — 3 identified callers in 30 days."""
    from tier_registry import TIER_LIMITS
    anon = TIER_LIMITS["anonymous"]["mcp_daily"]
    free = TIER_LIMITS["free"]["mcp_daily"]
    assert free > anon, f"free ({free}) must beat anonymous ({anon})"


def test_the_ladder_is_monotonic():
    """anon < free < identified < starter < developer."""
    from tier_registry import TIER_LIMITS
    rungs = ["anonymous", "free", "identified", "starter", "developer"]
    vals = [TIER_LIMITS[r]["mcp_daily"] for r in rungs]
    assert vals == sorted(vals) and len(set(vals)) == len(vals), \
        f"ladder not strictly increasing: {dict(zip(rungs, vals))}"


# ── 3. the published ladder tells the truth ───────────────────────────

def test_no_tier_publishes_a_null_daily_cap():
    """'anon' is an alias with no TIER_LIMITS entry, so /api/v1/tiers
    published "anon: calls_per_day=null" — reads as an UNCAPPED anonymous
    tier. It is capped; the published ladder was what lied."""
    from tier_registry import as_public_dict
    pub = as_public_dict()["tiers"]
    missing = [n for n, t in pub.items() if t.get("calls_per_day") is None]
    assert not missing, f"tiers publishing a null daily cap: {missing}"


def test_shipped_paywall_copy_states_the_enforced_anonymous_quota():
    """The quota is hand-copied into agent-facing upsell copy in ~26 places.
    If enforcement moves and the copy does not, we ship a false claim to every
    agent that hits a paywall — and agents quote this copy to their humans."""
    from tier_registry import TIER_LIMITS
    anon = TIER_LIMITS["anonymous"]["mcp_daily"]
    src = _src("routes", "paywall_hint_middleware.py")
    shipped = [ln for ln in src.splitlines()
               if re.search(r"anonymous\s*=?\s*[0-9,]+/day", ln, re.I)
               and not ln.lstrip().startswith("#")]
    assert shipped, "no shipped anonymous-quota copy found — did it move?"
    for ln in shipped:
        m = re.search(r"anonymous\s*=?\s*([0-9,]+)/day", ln, re.I)
        stated = int(m.group(1).replace(",", ""))
        assert stated == anon, (
            f"copy states anonymous {stated}/day but enforcement is "
            f"{anon}/day: {ln.strip()[:90]}")


def test_aliases_publish_their_api_tier_limit():
    from tier_registry import as_public_dict, TIER_LIMITS
    pub = as_public_dict()["tiers"]
    assert pub["anon"]["calls_per_day"] == TIER_LIMITS["anonymous"]["mcp_daily"]
    assert pub["founding"]["calls_per_day"] == TIER_LIMITS["founding"]["mcp_daily"]
