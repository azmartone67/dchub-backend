"""_facility_count_notes.primary must describe the basis the response ACTUALLY uses.

2026-08-25: `primary` read "distinct sites after cross-source de-duplication
(duplicate_of_id IS NULL)" (live 17,170) while `total_facilities` and
`facilities_distinct` in the SAME /api/v1/stats response were
COUNT(DISTINCT canonical_slug) (live 18,858), and `facilities_count_basis`
already declared 'canonical_slug_distinct'. Nothing guarded the pair, so the
prose and the number drifted apart silently.

This asserts on the AST of the response-building code in main.py — NOT on a
docstring or a comment. A guard that reads prose passes on deleted behaviour.
"""
import ast
import io
import os

import pytest

MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")


def _tree():
    return ast.parse(io.open(MAIN, encoding="utf-8").read())


def _assigned_value(tree, key):
    """Value node of `stats['<key>'] = ...`, or None."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if (
                isinstance(tgt, ast.Subscript)
                and isinstance(tgt.value, ast.Name)
                and tgt.value.id == "stats"
                and isinstance(tgt.slice, ast.Constant)
                and tgt.slice.value == key
            ):
                return node.value
    return None


def _primary_node(tree):
    notes = _assigned_value(tree, "_facility_count_notes")
    assert notes is not None, "stats['_facility_count_notes'] assignment not found in main.py"
    assert isinstance(notes, ast.Dict), "_facility_count_notes is no longer a dict literal"
    for k, v in zip(notes.keys, notes.values):
        if isinstance(k, ast.Constant) and k.value == "primary":
            return v
    pytest.fail("_facility_count_notes has no 'primary' key")


def test_primary_is_conditional_on_the_canonical_read():
    """A hardcoded string would re-describe the fallback as the primary basis."""
    node = _primary_node(_tree())
    assert isinstance(node, ast.IfExp), (
        "'primary' must be a conditional expression keyed on the canonical read, "
        "not a fixed string — a fixed string cannot stay true across the fallback."
    )
    names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
    assert "_canon_distinct" in names, (
        "'primary' must branch on _canon_distinct — the same variable "
        "facilities_count_basis branches on. Got names: %r" % (sorted(names),)
    )


def test_primary_branches_name_their_own_basis():
    node = _primary_node(_tree())
    canon = ast.literal_eval(node.body)
    fallback = ast.literal_eval(node.orelse)

    assert "canonical_slug" in canon, (
        "canonical branch must name COUNT(DISTINCT canonical_slug); got: %r" % (canon[:120],)
    )
    assert "duplicate_of_id" not in canon, (
        "canonical branch must NOT name duplicate_of_id — that is the fallback "
        "basis (live 17,170) and naming it here is the original bug; got: %r" % (canon[:160],)
    )
    assert "duplicate_of_id" in fallback, (
        "fallback branch must name duplicate_of_id IS NULL; got: %r" % (fallback[:120],)
    )


def test_count_basis_field_branches_on_the_same_variable():
    """facilities_count_basis and primary must not be able to disagree."""
    node = _assigned_value(_tree(), "facilities_count_basis")
    assert node is not None, "stats['facilities_count_basis'] assignment not found"
    assert isinstance(node, ast.IfExp), "facilities_count_basis is expected to be conditional"
    names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
    assert "_canon_distinct" in names, (
        "facilities_count_basis must branch on _canon_distinct so it cannot "
        "disagree with _facility_count_notes.primary; got: %r" % (sorted(names),)
    )


def test_verified_key_is_annotated_as_a_dedup_state():
    """'verified' means DIFFERENT counts on /stats and /stats/canonical.

    /api/v1/stats        discovered_verified  = COUNT(*) WHERE is_duplicate = 0
    /api/v1/stats/canonical facilities_verified = COUNT(*) WHERE duplicate_of_id IS NULL

    stats_canonical's provenance says neither predicate may be published as a
    source verification. The key cannot be renamed without breaking readers, so
    the basis must be named alongside it.
    """
    notes = _assigned_value(_tree(), "_facility_count_notes")
    basis_map = None
    for k, v in zip(notes.keys, notes.values):
        if isinstance(k, ast.Constant) and k.value == "basis_map":
            basis_map = v
    assert basis_map is not None, "_facility_count_notes must carry a basis_map"

    entry = None
    for k, v in zip(basis_map.keys, basis_map.values):
        if isinstance(k, ast.Constant) and k.value == "discovered_verified":
            entry = ast.literal_eval(v)
    assert entry is not None, "basis_map must document discovered_verified"
    assert "is_duplicate = 0" in entry, (
        "basis_map must state the literal predicate for discovered_verified; got: %r" % (entry[:160],)
    )
    assert "not" in entry.lower() and "verif" in entry.lower(), (
        "basis_map must warn that discovered_verified is NOT a source "
        "verification; got: %r" % (entry[:200],)
    )
