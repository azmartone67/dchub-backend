"""Guards for routes/anchor_intents.py — the single publication point.

The six anchor intents are published verbatim on 13 /for/<platform> pages,
llms.txt, llms-full.txt, AGENTS.md, all six /integrations pages, and in the
starter pack every connecting agent receives. Agents copy them literally, so
they are a public API written in English.

They previously lived in three independent transcriptions, which had already
diverged: the gateway carried five of the six, so "which ISO has the shortest
time-to-power right now" was published on every page and reached no agent.
Nothing detected it, because each copy was internally consistent.

These tests exist to make sure the derivation stays a derivation. The one that
matters is test_page_is_not_a_second_transcription — a future edit that
re-hardcodes the list into the page would leave every other assertion here
passing, which is exactly how the original drift happened.

Pure functions: no DB, no network, and never imports main (tests/ must not).
"""
import re

import pytest

ai = pytest.importorskip("routes.anchor_intents")


def test_anchors_are_well_formed():
    assert len(ai.ANCHORS) >= 5
    for a in ai.ANCHORS:
        assert a["recipe"] and isinstance(a["recipe"], str)
        assert a["intent"] and isinstance(a["intent"], str)
        assert len(a["intent"]) > 20, f"suspiciously short intent: {a['intent']!r}"


def test_intents_are_passthrough_questions_not_api_calls():
    """An anchor is what a USER asks. Naming our own tools in it teaches agents
    to paste our API instead of their user's question."""
    for a in ai.ANCHORS:
        assert not re.search(r"execute_plan|plan_query|get_[a-z_]+|rank_markets",
                             a["intent"]), a["intent"]


def test_intents_are_unique():
    assert len(set(ai.INTENTS)) == len(ai.INTENTS)


def test_contract_hash_is_stable_and_order_sensitive():
    """A consumer compares the hash to decide whether to re-derive. It must not
    change when nothing changed, and it MUST change when the order does —
    the sequence is the decision flow, so reordering is a contract change."""
    assert ai.contract_hash() == ai.contract_hash()
    assert len(ai.contract_hash()) == 16

    original = ai.ANCHORS
    try:
        ai.ANCHORS = tuple(reversed(original))
        assert ai.contract_hash() != _hash_of(original), \
            "reordering the anchors must change the contract hash"
    finally:
        ai.ANCHORS = original


def _hash_of(anchors):
    import hashlib
    import json
    canon = json.dumps([[a["recipe"], a["intent"]] for a in anchors],
                       separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def test_payload_carries_what_a_consumer_needs_to_derive():
    p = ai.anchor_payload()
    for k in ("contract_hash", "count", "call", "param", "anchors", "source"):
        assert k in p, f"payload missing {k}"
    assert p["count"] == len(p["anchors"]) == len(ai.ANCHORS)
    assert p["call"] == "execute_plan" and p["param"] == "intent"
    # `source` must name the file to edit — the whole point is one answer.
    assert "anchor_intents.py" in p["source"]


def test_rendered_html_contains_every_anchor_recipe_first():
    """Recipe name leads, question follows: a client truncating a long block
    clips the TAIL, so whatever leads the line is what survives."""
    html = ai.render_anchor_list_html()
    assert html.count("<li>") == len(ai.ANCHORS)
    for a in ai.ANCHORS:
        assert a["recipe"] in html
        assert a["intent"] in html
        assert html.index(a["recipe"]) < html.index(a["intent"])


def test_front_door_block_is_not_a_second_transcription():
    """THE test. _FRONT_DOOR_HTML must SUBSTITUTE the canonical list, never
    restate it. If someone re-hardcodes the intents into that block, every other
    assertion in this file still passes and the drift is invisible again —
    which is precisely how the original three-copy divergence happened.

    Scoped to the _FRONT_DOOR_HTML literal on purpose. The same file also
    carries the "AI Campus Power pack" pane, which is a SEPARATE published
    artifact (`dchub://packs/ai-campus-power`) with its own six intents. Two of
    those coincide with anchors; three deliberately differ. Asserting over the
    whole file would conflate two contracts and force one of them to be wrong.
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "routes" / "integrations_landing.py").read_text(encoding="utf-8")
    assert "anchor_intents" in src, \
        "the page no longer derives from the canonical module"

    start = src.index('_FRONT_DOOR_HTML = """')
    block = src[start:src.index('"""', start + 25)]
    assert "__ANCHOR_LIST__" in block, \
        "_FRONT_DOOR_HTML lost its substitution slot — the list was re-inlined"

    # One worked example (the execute_plan literal) is intentional; a LIST is not.
    inlined = sum(block.count(a["intent"]) for a in ai.ANCHORS)
    assert inlined <= 1, (
        f"{inlined} anchor intents appear literally inside _FRONT_DOOR_HTML — "
        "the list must be substituted from routes/anchor_intents.py, not restated"
    )


def test_campus_pack_is_a_distinct_contract_not_stale_anchors():
    """The AI Campus Power pack pane lists its own six intents. This asserts it
    stays a DISTINCT artifact rather than quietly becoming a stale copy of the
    anchors: if it ever matches the anchor set exactly, it should be derived
    from the canonical module instead of maintained separately.
    """
    import pathlib
    import re
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "routes" / "integrations_landing.py").read_text(encoding="utf-8")
    m = re.search(r"Starter pack (?:&mdash;|\u2014|-) AI Campus Power.*?<ul[^>]*>(.*?)</ul>",
                  src, re.S)
    assert m, ("campus-pack pane not found — if it was renamed, update this "
               "regex; a silently skipped test guards nothing")
    pack = set(re.findall(r"<li>&ldquo;(.*?)&rdquo;</li>", m.group(1)))
    anchors = set(ai.INTENTS)
    assert pack, "campus pack pane has no intents"
    assert pack != anchors, (
        "the campus pack now matches the anchor set exactly — derive it from "
        "routes/anchor_intents.py rather than maintaining a second copy"
    )
