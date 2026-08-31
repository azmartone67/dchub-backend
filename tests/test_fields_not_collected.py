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
