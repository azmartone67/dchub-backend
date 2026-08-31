"""
r-named-absence (2026-08-31) — v6 FIELDS_NOT_COLLECTED, pinned.

Why this exists: a user spent 2,563 API calls on 2026-08-01 hunting
per-facility PUE, then left saying the values "did not appear to be accurate or
reliable". PUE is not a field DC Hub carries at all. out_of_scope already
listed PUE — but only as a DEFINITIONS question ("what is PUE"); asking for PUE
VALUES is in_scope by topic and was never refused, so nothing ever told him.

The `instead` pointers are the part most likely to rot into a lie under a
well-meaning future edit, so they are pinned hardest: `instead` must never
offer a substitute FOR the missing field.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from routes.problem_taxonomy import (
    FIELDS_NOT_COLLECTED, FIELDS_NOT_COLLECTED_NOTE, IN_SCOPE,
    TAXONOMY_VERSION, contract_hash, taxonomy_payload,
)

ALIASES = {a for f in FIELDS_NOT_COLLECTED for a in f["aliases"]}


def test_version_is_at_least_v6():
    assert TAXONOMY_VERSION >= 6


def test_pue_is_named_absent_by_the_word_users_actually_type():
    assert "pue" in ALIASES
    assert "power usage effectiveness" in ALIASES


def test_every_entry_is_complete():
    for f in FIELDS_NOT_COLLECTED:
        assert f["field"] and f["why"] and f["instead"], f
        assert f["aliases"], f
        assert all(a == a.lower() for a in f["aliases"]), \
            f"aliases must be lowercase for case-insensitive matching: {f}"


def test_instead_never_offers_a_substitute_for_the_missing_field():
    """The honesty pin. A cooling TYPE is not a PUE. If a future edit makes
    `instead` mention the absent term itself, it has started implying we can
    approximate it — which is the overclaiming this whole registry refuses."""
    for f in FIELDS_NOT_COLLECTED:
        low = f["instead"].lower()
        for alias in f["aliases"]:
            assert alias not in low, (
                f"`instead` for {f['field']!r} mentions its own missing term "
                f"{alias!r} — it must point at a DIFFERENT real field, not a "
                f"stand-in for the one we do not have")


def test_absence_is_distinct_from_out_of_scope():
    """These are fields missing INSIDE covered topics — not wrong questions.
    If this registry ever became a synonym for out_of_scope it would stop
    doing the job it was added for."""
    from routes.problem_taxonomy import OUT_OF_SCOPE
    fields = {f["field"].lower() for f in FIELDS_NOT_COLLECTED}
    assert not (fields & {o.lower() for o in OUT_OF_SCOPE})
    assert IN_SCOPE, "the registry only means anything against a positive list"


def test_it_is_published_in_the_payload():
    p = taxonomy_payload()
    assert len(p["fields_not_collected"]) == len(FIELDS_NOT_COLLECTED)
    assert p["fields_not_collected_note"] == FIELDS_NOT_COLLECTED_NOTE
    assert isinstance(p["fields_not_collected"][0]["aliases"], list), \
        "aliases must serialise as a JSON array, not a tuple"


def test_it_participates_in_contract_hash():
    """A consumer caching the routing map must re-derive when an absence is
    declared. If this stops being hashed, stale clients keep probing for a
    field we have since published as missing — the exact 2,563-call failure."""
    import routes.problem_taxonomy as t
    before = contract_hash()
    original = t.FIELDS_NOT_COLLECTED
    try:
        t.FIELDS_NOT_COLLECTED = original + (
            {"field": "x", "aliases": ("x",), "why": "y", "instead": "z"},)
        assert contract_hash() != before, \
            "adding a declared absence MUST change contract_hash"
    finally:
        t.FIELDS_NOT_COLLECTED = original
    assert contract_hash() == before, "hash must be restored"


# ---------------------------------------------------------------------------
# r-absence-on-site (2026-08-31): the HTML render.
#
# v6 put named absence in the JSON payload, which reaches AGENTS. The person
# deciding whether to build on DC Hub reads a PAGE — the user who spent 2,563
# calls hunting PUE was a human evaluating us for a comparison site.
# ---------------------------------------------------------------------------

from routes.problem_taxonomy import (  # noqa: E402
    render_scope_html, OUT_OF_SCOPE, NOT_FOR_NOTE,
)


def test_the_page_names_every_absent_field():
    """Compared against the ESCAPED form — `why` for SMR contains 'nuclear' in
    single quotes, which html.escape turns into &#x27;. Asserting on the raw
    string would fail for the right reason and look like a render bug."""
    from html import escape
    html = render_scope_html()
    for f in FIELDS_NOT_COLLECTED:
        assert escape(f["field"]) in html, f"{f['field']} missing from the page"
        assert escape(f["why"]) in html
        assert escape(f["instead"]) in html


def test_pue_is_visible_to_a_human_reading_the_page():
    assert "PUE" in render_scope_html()


def test_the_two_negative_sections_stay_distinct():
    """THE PIN THAT MATTERS.

    Conflating these is the exact filing error that caused the incident: PUE
    sat in out_of_scope as a DEFINITIONS question, so asking for PUE VALUES was
    never refused. "Wrong question" and "right question, no such field" must
    render as two different things with two different headings.
    """
    html = render_scope_html()
    assert "Not a DC Hub question" in html
    assert "A DC Hub question we cannot answer" in html
    assert html.index("Not a DC Hub question") < html.index("A DC Hub question we cannot answer")

    # and the two lists must not have been merged into one <ul>
    out_of_scope_first = html.index(OUT_OF_SCOPE[0])
    absence_first = html.index(FIELDS_NOT_COLLECTED[0]["field"])
    between = html[out_of_scope_first:absence_first]
    assert "</ul>" in between, "the two negative lists collapsed into one"


def test_notes_render_backticks_as_code_not_literal_ticks():
    """The notes are authored once and read by both a JSON consumer and this
    page. Backticks are right there and wrong here."""
    html = render_scope_html()
    assert "<code>instead</code>" in html
    assert "`" not in html, "literal backtick leaked into the page"


def test_render_escapes_before_substituting():
    """Order pin: escaping AFTER the backtick substitution would eat the tags
    it just produced. Any raw < or > from the source data would prove the
    escape ran at the wrong time."""
    import routes.problem_taxonomy as t
    original = t.FIELDS_NOT_COLLECTED
    try:
        t.FIELDS_NOT_COLLECTED = ({
            "field": "<script>x</script>",
            "aliases": ("x",),
            "why": "a & b",
            "instead": "`code` and <b>bold</b>",
        },)
        html = render_scope_html()
        assert "<script>" not in html, "unescaped markup reached the page"
        assert "&lt;script&gt;" in html
        assert "&amp;" in html
        assert "<code>code</code>" in html, "backtick substitution stopped working"
        assert "<b>bold</b>" not in html, "raw markup survived escaping"
    finally:
        t.FIELDS_NOT_COLLECTED = original
