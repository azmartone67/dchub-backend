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


def test_why_live_reasons_is_a_small_stable_enum():
    """ChatGPT round-11: an ENUMERATED reason set, not free text — the codes
    the planner stamps on replays only become a countable corpus if the value
    space is small and stable. Codes snake_case; phrases digit-free (same
    count-free rule as the lists); the set stays SMALL by assertion, because
    an enum that grows a value per class stops being an aggregation axis.
    """
    assert 4 <= len(pt.WHY_LIVE_REASONS) <= 12
    for code, phrase in pt.WHY_LIVE_REASONS.items():
        assert re.fullmatch(r"requires_[a-z_]+", code), f"non-enum code: {code!r}"
        assert isinstance(phrase, str) and len(phrase) >= 20
        assert not re.search(r"\d", phrase), f"digit in phrase for {code}"
    assert len(set(pt.WHY_LIVE_REASONS.values())) == len(pt.WHY_LIVE_REASONS)


def test_payload_carries_the_reason_enum():
    p = pt.taxonomy_payload()
    assert p["why_live_reasons"] == dict(pt.WHY_LIVE_REASONS)
    assert list(p["why_live_reasons"]) == list(pt.WHY_LIVE_REASONS)  # order kept


def test_contract_hash_is_sensitive_to_the_reason_enum():
    original = pt.WHY_LIVE_REASONS
    try:
        pt.WHY_LIVE_REASONS = {**original, "requires_something_new": "requires a brand-new live layer"}
        grown = pt.contract_hash()
    finally:
        pt.WHY_LIVE_REASONS = original
    assert grown != pt.contract_hash(), \
        "adding an enum value must change the contract hash"


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


# ══ COVERAGE (v3, 2026-08-10) ═══════════════════════════════════════════════
# IN_SCOPE answers "is this a DC Hub question?". COVERAGE answers the question
# a router actually has: "can you answer MY question, in how many steps, and
# what will you refuse to tell me?" A tool COUNT cannot answer that at all,
# which is why an agent that only sees "82 tools" falls back to training data.
#
# The tests that matter here are the HONESTY ones — that the table cannot
# become a second vocabulary (problem must be an IN_SCOPE member), cannot
# advertise tools that do not exist, and cannot quietly drop its published
# limits while every other assertion keeps passing.


def test_every_covered_problem_is_an_in_scope_term():
    """THE anti-drift guard. If coverage could name a problem that is not in
    IN_SCOPE, this table becomes the fourth independent transcription of the
    routing vocabulary — the exact disease this module was created to cure.
    """
    for c in pt.COVERAGE:
        assert c["problem"] in pt.IN_SCOPE, (
            f"{c['problem']!r} is not an IN_SCOPE term — coverage must derive "
            "from the taxonomy, never extend it"
        )


def test_coverage_entries_are_well_formed():
    assert len(pt.COVERAGE) >= 8
    seen = set()
    for c in pt.COVERAGE:
        assert set(c) == {"problem", "entry_tool", "workflow", "status", "limits"}
        assert c["problem"] not in seen, f"duplicate problem: {c['problem']}"
        seen.add(c["problem"])
        assert isinstance(c["workflow"], tuple) and c["workflow"]
        assert isinstance(c["limits"], tuple)
        assert c["status"] in pt.COVERAGE_STATUSES


def test_tool_names_look_like_tool_names():
    """entry_tool and every workflow step must be snake_case identifiers. A
    prose phrase here would render as a call an agent cannot make.
    """
    for c in pt.COVERAGE:
        for name in (c["entry_tool"],) + tuple(c["workflow"]):
            assert re.fullmatch(r"[a-z][a-z0-9_]+", name), f"not a tool name: {name!r}"


def test_step_count_is_derived_not_hand_written():
    """A hand-maintained count is exactly the kind of fact that rots while
    every surface around it stays internally consistent — the count-free rule,
    applied to the one number this payload legitimately carries.
    """
    for c, payload in zip(pt.COVERAGE, pt.coverage_payload()):
        assert payload["step_count"] == len(c["workflow"])


def test_the_known_proxies_are_declared_as_proxies():
    """The limits are the point. These two are REAL, already-published
    limitations quoted from the tools' own responses — if a future edit
    quietly drops them to make the table look stronger, that is the failure
    this test exists to catch.
    """
    by_problem = {c["problem"]: c for c in pt.COVERAGE}

    ai = by_problem["AI/GPU compute campuses"]
    assert ai["status"] == "expanding"
    joined = " ".join(ai["limits"]).lower()
    assert "proxy" in joined, "ai_ready_mw is a proxy and must say so"
    assert "density" in joined, "the missing per-rack density input must be named"

    mw = by_problem["megawatts and power density"]
    assert mw["status"] == "partial"
    assert "presence" in " ".join(mw["limits"]).lower(), (
        "facility_count is a PRESENCE signal, not a capacity one — say it"
    )


def test_a_status_that_claims_more_must_earn_it():
    """`expanding` and `partial` MEAN something: a named input is a proxy, or
    part of the question is uncovered. Either claim without a published limit
    is an empty label.
    """
    for c in pt.COVERAGE:
        if c["status"] in ("expanding", "partial"):
            assert c["limits"], (
                f"{c['problem']!r} is {c['status']} but publishes no limit — "
                "say what is missing or call it mature"
            )


def test_limits_are_prose_not_marketing_stubs():
    for c in pt.COVERAGE:
        for lim in c["limits"]:
            assert len(lim) >= 40, f"stub limit on {c['problem']!r}: {lim!r}"
            assert not lim.endswith("!"), "a limit is a fact, not a pitch"


def test_coverage_payload_shape():
    payload = pt.coverage_payload()
    assert len(payload) == len(pt.COVERAGE)
    for row in payload:
        assert set(row) == {"problem", "entry_tool", "workflow", "step_count",
                            "status", "limits", "has_published_limits"}
        assert isinstance(row["workflow"], list)   # JSON-safe, not a tuple
        assert isinstance(row["limits"], list)
        assert row["has_published_limits"] == bool(row["limits"])


def test_taxonomy_payload_carries_coverage():
    p = pt.taxonomy_payload()
    assert p["version"] == 3
    assert p["coverage"] == pt.coverage_payload()
    assert p["coverage_statuses"] == list(pt.COVERAGE_STATUSES)
    assert "tool count" in p["coverage_note"].lower()


def test_contract_hash_is_sensitive_to_coverage():
    """A consumer caching on contract_hash must re-derive when a LIMIT changes
    or a status moves — those are the parts an agent routes on, so a silent
    change is precisely the drift this module exists to prevent.
    """
    before = pt.contract_hash()
    original = pt.COVERAGE
    try:
        mutated = list(original)
        first = dict(mutated[0])
        first["limits"] = tuple(list(first["limits"]) + [
            "a newly published limit that a consumer must notice and re-derive on"])
        mutated[0] = first
        pt.COVERAGE = tuple(mutated)
        assert pt.contract_hash() != before
    finally:
        pt.COVERAGE = original
    assert pt.contract_hash() == before


def test_coverage_endpoint_serves_the_module_payload():
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    app.register_blueprint(pt.problem_taxonomy_bp)
    client = app.test_client()

    r = client.get("/api/v1/canon/coverage")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["version"] == pt.TAXONOMY_VERSION
    assert body["contract_hash"] == pt.contract_hash()
    assert body["coverage"] == pt.coverage_payload()
    # Every status in the table must be explained in the payload itself — an
    # agent should not have to guess what "partial" means.
    for c in pt.COVERAGE:
        assert c["status"] in body["statuses"]
    assert "public" in r.headers.get("Cache-Control", "")
