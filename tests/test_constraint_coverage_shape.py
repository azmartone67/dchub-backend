"""`constraint_coverage` ships in four shapes — every emitter must name its own.

Measured live 2026-08-25, all on non-errored calls:

    get_power_availability_timeline  list[str]
    rank_sites                       {"power_score": "unavailable"}
    site_selection_canvas            {"capacity_mw": {applied, reason, instead}}
    cross_layer_sites                {"headroom_mw": {status, reason}}

One name, four incompatible types. An agent iterating the list form over an
object form gets dict KEYS and no error — a silent wrong-type read, which is
worse than a missing block because it looks like it worked.

This does NOT unify them (breaking, and they carry genuinely different
information). It makes the shape machine-readable so consumers branch.
"""
import ast
import io
import os
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_ROUTES = os.path.join(_ROOT, "routes")

import sys
sys.path.insert(0, _ROOT)
from util.constraint_coverage_shape import (  # noqa: E402
    ARGUMENT_DISPOSITION, CAVEAT_LIST, EMPTY, FIELD_STATUS_DETAIL,
    FIELD_STATUS_MAP, SHAPE_LEGEND, SHAPES, UNKNOWN, annotate, shape_of,
)


# ── the derivation itself ────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (["generation != deliverable load"],                       CAVEAT_LIST),
    ({"power_score": "unavailable"},                           FIELD_STATUS_MAP),
    ({"capacity_mw": {"applied": False, "reason": "r"}},       ARGUMENT_DISPOSITION),
    ({"headroom_mw": {"status": "unavailable", "reason": "r"}}, FIELD_STATUS_DETAIL),
    ([], EMPTY), ({}, EMPTY), (None, EMPTY),
    ({"a": 1}, UNKNOWN), ("a string", UNKNOWN), (7, UNKNOWN),
])
def test_shape_is_derived_from_the_value(value, expected):
    assert shape_of(value) == expected


def test_applied_beats_status_because_a_dropped_argument_matters_more():
    """A block carrying BOTH must report argument_disposition.

    `applied:false` means an argument the SCHEMA ACCEPTED was then not used.
    That is the thing an agent most needs to notice, so it wins over the
    generic field-status reading.
    """
    both = {"capacity_mw": {"applied": False, "status": "unavailable", "reason": "r"}}
    assert shape_of(both) == ARGUMENT_DISPOSITION


def test_every_shape_has_a_published_legend():
    """The vocabulary ships with the value — not in a changelog."""
    assert set(SHAPE_LEGEND) == set(SHAPES)
    for name, text in SHAPE_LEGEND.items():
        assert len(text) > 20, f"{name} legend is too thin to act on"


def test_annotate_is_a_noop_without_the_key():
    """An emitter that publishes no coverage must not grow an orphan shape."""
    assert annotate({"ok": True}) == {"ok": True}
    assert "constraint_coverage_shape" not in annotate({"ok": True})


def test_annotate_stamps_next_to_the_value():
    out = annotate({"constraint_coverage": ["a"]})
    assert out["constraint_coverage_shape"] == CAVEAT_LIST


# ── the fleet-wide invariant ─────────────────────────────────────────────────

def _route_files():
    for fn in sorted(os.listdir(_ROUTES)):
        if fn.endswith(".py"):
            yield fn, os.path.join(_ROUTES, fn)


def _emits_coverage(tree):
    """True if this module puts a 'constraint_coverage' key into a payload.

    AST, not grep: the string appears in docstrings and prose all over this
    codebase, and a grep-based guard would pass on a module that only TALKS
    about coverage — the vacuous-guard class this repo keeps re-learning.
    """
    for node in ast.walk(tree):
        # {"constraint_coverage": ...} inside a dict literal
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant) and k.value == "constraint_coverage":
                    return True
        # payload["constraint_coverage"] = ...
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            if node.slice.value == "constraint_coverage":
                return True
    return False


def _uses_payload_annotate(tree):
    """Module stamps at PAYLOAD level via annotate() / its fail-soft alias.

    That covers every key in the payload at once, so per-emission pairing is
    not required when it is present.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            # Both the direct name and the fail-soft alias. Routes import this
            # helper defensively (`try: … except: _cc_annotate = None`) because
            # test_cross_layer_public_reason_hygiene execs routes with all
            # first-party imports BLOCKED — a hard import breaks that harness.
            if node.func.id in ("annotate", "_cc_annotate"):
                return True
    return False


def _unstamped_emissions(tree):
    """Emissions of constraint_coverage that carry no shape beside them.

    ★ PER-EMISSION, not per-file. Mutation-tested 2026-08-25: a per-file check
    passed when cross_layer_sites lost the stamp on its SUCCESS path, because
    its error path still had one. A module with two emissions and one stamp is
    exactly the case that ships a shapeless payload.
    """
    if _uses_payload_annotate(tree):
        return []
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if "constraint_coverage" in keys and "constraint_coverage_shape" not in keys:
                bad.append(node.lineno)
    # payload["constraint_coverage"] = … must have a sibling subscript assign
    subs = {n.slice.value for n in ast.walk(tree)
            if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
            and isinstance(n.slice.value, str)}
    if "constraint_coverage" in subs and "constraint_coverage_shape" not in subs:
        bad.append(-1)
    return bad


def test_the_scan_actually_finds_the_known_emitters():
    """Guard the guard.

    If the AST walk silently stopped matching, every assertion below would pass
    over an empty set. Pin the four emitters measured on 2026-08-25.
    """
    found = {fn for fn, path in _route_files()
             if _emits_coverage(ast.parse(io.open(path, encoding="utf-8").read()))}
    for expected in ("power_availability_timeline.py", "site_selection_canvas.py",
                     "cross_layer_sites.py", "interconnection_queues.py"):
        assert expected in found, f"scan no longer sees {expected} as an emitter"


def test_every_emitter_names_its_shape():
    missing = []
    for fn, path in _route_files():
        src = io.open(path, encoding="utf-8").read()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        if _emits_coverage(tree):
            bad = _unstamped_emissions(tree)
            if bad:
                missing.append("%s (lines %r)" % (fn, bad))
    assert not missing, (
        "these modules emit constraint_coverage without naming its shape: %r — "
        "one name already means four incompatible types, so an unnamed fifth is "
        "a silent wrong-type read waiting to happen" % (missing,)
    )
