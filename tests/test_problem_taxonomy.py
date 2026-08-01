"""Guards for routes/problem_taxonomy.py — the canonical problem taxonomy.

Two published lists: IN_SCOPE ("this is a DC Hub question" — the routing
vocabulary) and OUT_OF_SCOPE ("when NOT to use DC Hub" — the negative examples
classifiers need). They render on llms.txt, 13 /for/* pages, /integrations/*,
inside the MCP initialize instructions, and in discover_tools output. The
positive vocabulary previously lived in three independent transcriptions that
had already drifted (frontend heal TRIGGERS vs gateway execute_plan description
vs this repo's front-door pane) with nothing detecting it.

These tests keep the derivation a derivation. The one that matters is
test_front_door_scope_is_not_a_second_transcription — a future edit that
re-hardcodes the vocabulary into the page leaves every other assertion passing,
which is exactly how the original drift happened.

Pure functions: no DB, no network, and never imports main (tests/ must not).
"""
import re

import pytest

pt = pytest.importorskip("routes.problem_taxonomy")


def test_lists_are_well_formed():
    assert len(pt.IN_SCOPE) >= 8
    assert len(pt.OUT_OF_SCOPE) >= 5
    for term in list(pt.IN_SCOPE) + list(pt.OUT_OF_SCOPE):
        assert term and isinstance(term, str)
        assert len(term) >= 10, f"suspiciously short entry: {term!r}"
    assert len(set(pt.IN_SCOPE)) == len(pt.IN_SCOPE)
    assert len(set(pt.OUT_OF_SCOPE)) == len(pt.OUT_OF_SCOPE)
    assert pt.NOT_FOR_NOTE and len(pt.NOT_FOR_NOTE) > 40


def test_lists_are_count_free():
    """THE brand rule. A number inside a taxonomy entry would rot on its own
    schedule — the entire point of the derive chain is that nothing here can
    rot. Digit-free is deliberately stricter than "no canon phrases": if a
    future entry truly needs a digit, loosen this test in the same PR and say
    why.
    """
    for term in list(pt.IN_SCOPE) + list(pt.OUT_OF_SCOPE) + [pt.NOT_FOR_NOTE]:
        assert not re.search(r"\d", term), f"count/digit in taxonomy entry: {term!r}"


def test_out_of_scope_covers_the_classes_partners_named():
    """The negative list exists so an agent can point "what is a UPS" at it.
    Pin the CLASSES (definitional, networking, chip benchmarks, EE theory,
    AI-model advice, cloud pricing) without transcribing the entries.
    """
    joined = " ".join(pt.OUT_OF_SCOPE)
    for marker in ("PUE", "UPS", "networking", "benchmarks", "theory",
                   "cloud", "LLM"):
        assert marker in joined, f"negative list lost its {marker!r} class"


def test_contract_hash_is_stable_and_content_sensitive():
    assert pt.contract_hash() == pt.contract_hash()
    assert len(pt.contract_hash()) == 16
    original = pt.IN_SCOPE
    try:
        pt.IN_SCOPE = tuple(reversed(original))
        reordered = pt.contract_hash()
    finally:
        pt.IN_SCOPE = original
    assert reordered != pt.contract_hash(), \
        "reordering in_scope must change the contract hash — order is part of the contract"


def test_payload_carries_what_a_consumer_needs_to_derive():
    p = pt.taxonomy_payload()
    for k in ("version", "contract_hash", "source", "in_scope",
              "out_of_scope", "not_for_note"):
        assert k in p, f"payload missing {k}"
    assert p["in_scope"] == list(pt.IN_SCOPE)
    assert p["out_of_scope"] == list(pt.OUT_OF_SCOPE)
    assert p["contract_hash"] == pt.contract_hash()
    # `source` must name the file to edit — the whole point is one answer.
    assert "problem_taxonomy.py" in p["source"]


def test_in_scope_sentence_joins_every_term_in_order():
    s = pt.in_scope_sentence()
    pos = -1
    for term in pt.IN_SCOPE:
        i = s.find(term)
        assert i > pos, f"{term!r} missing or out of order in the sentence"
        pos = i
    assert ", or " in s  # the closing joiner — one place decides the commas


def test_rendered_scope_html_carries_both_lists():
    html = pt.render_scope_html()
    from html import escape
    assert escape(pt.in_scope_sentence()) in html
    for entry in pt.OUT_OF_SCOPE:
        assert escape(entry) in html
    assert escape(pt.NOT_FOR_NOTE) in html
    assert "Not a DC Hub question" in html


def test_endpoint_serves_the_module_payload():
    """A minimal app (never main) proves the blueprint serves EXACTLY the
    module's payload — the endpoint is what the frontend heal and the
    mcp-server daily snapshot read.
    """
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(pt.problem_taxonomy_bp)
    r = app.test_client().get("/api/v1/canon/taxonomy")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    expected = pt.taxonomy_payload()
    for k, v in expected.items():
        assert body[k] == v
    assert "max-age=3600" in r.headers.get("Cache-Control", "")


def test_front_door_scope_is_not_a_second_transcription():
    """THE test. _FRONT_DOOR_HTML must SUBSTITUTE the scope block, never
    restate the vocabulary. If someone re-hardcodes the trigger list into that
    literal, every other assertion here still passes and the drift is invisible
    again — precisely how the original three-copy divergence happened.
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "routes" / "integrations_landing.py").read_text(encoding="utf-8")
    assert "problem_taxonomy" in src, \
        "the page no longer derives from the canonical taxonomy module"

    start = src.index('_FRONT_DOOR_HTML = """')
    block = src[start:src.index('"""', start + 25)]
    assert "__SCOPE_BLOCK__" in block, \
        "_FRONT_DOOR_HTML lost its scope substitution slot — the list was re-inlined"

    inlined = sum(block.count(term) for term in pt.IN_SCOPE)
    assert inlined <= 1, (
        f"{inlined} in-scope terms appear literally inside _FRONT_DOOR_HTML — "
        "the vocabulary must be substituted from routes/problem_taxonomy.py, "
        "not restated"
    )


def test_front_door_html_renders_the_scope_block():
    """The substituted result: the importable page constant carries the
    rendered vocabulary (fail-open would render it empty — this is the loud
    CI failure that contract chose).
    """
    il = pytest.importorskip("routes.integrations_landing")
    from html import escape
    assert "__SCOPE_BLOCK__" not in il._FRONT_DOOR_HTML
    assert escape(pt.in_scope_sentence()) in il._FRONT_DOOR_HTML
    assert "Not a DC Hub question" in il._FRONT_DOOR_HTML
    for entry in pt.OUT_OF_SCOPE:
        assert escape(entry) in il._FRONT_DOOR_HTML


def test_mcp_landing_carries_the_scope_pane():
    """/integrations + /integrations/mcp render MCP_LANDING_HTML, which does
    NOT embed _FRONT_DOOR_HTML — live-verified 2026-07-31 that the first page
    the round-10 spec names carried neither list until it got its own pane.
    Asserting on the page CONSTANT (what the route returns) so a lost
    substitution or a re-inlined copy both fail loudly.
    """
    il = pytest.importorskip("routes.integrations_landing")
    from html import escape
    html = il.MCP_LANDING_HTML
    assert "__SCOPE_PANE__" not in html
    assert 'id="scope"' in html
    assert escape(pt.in_scope_sentence()) in html
    assert "Not a DC Hub question" in html
    for entry in pt.OUT_OF_SCOPE:
        assert escape(entry) in html
