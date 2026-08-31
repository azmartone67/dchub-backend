"""surface_truth must delegate the floor rule, not carry its own copy.

The rule itself is tested in tests/test_canon_floor.py, by import. This file is
what remains here: proof that this shell uses the shared rule rather than a
private one.

WHY THAT MATTERS. This module originally banned the RANGE 19,000-23,999. Once
PINNED reached 18,500+, the live-healed floor 19,700+ sat inside both the accept
band and the ban, so every surface simultaneously PASSED "carries canon floor"
and FAILED "free of retired floors" on the identical bytes — three lanes red on
a contradiction. Fixing it here left copies of the same rotted regex in
seven_levers and intelligence_expansion, which then produced their own false
REDs. One rule, imported, is the only shape that does not rot back.
"""

import ast
import pathlib

import pytest

SRC = (pathlib.Path(__file__).resolve().parents[1]
       / "routes" / "surface_truth_master_shell.py")
TEXT = SRC.read_text()


def _code_only(text):
    """Comments stripped — a guard must not fire on the note explaining it."""
    return "\n".join(l.split("#")[0] for l in text.split("\n"))


def test_it_imports_the_shared_rule():
    assert "from util.canon_floor import" in TEXT
    for name in ("ACCEPT_MAX_MULT", "OVERCLAIM_MAX_MULT", "RETIRED_LITERALS",
                 "acceptable_floor", "retired_floors"):
        assert name in TEXT, f"{name} not taken from the shared rule"


def test_no_private_copy_of_the_rule_remains():
    code = _code_only(TEXT)
    assert "19|20|21|22|23" not in code, "the rotted range ban is back"
    assert "_OVERCLAIM_MAX_MULT = " not in code, "a local copy will drift"
    assert "_RETIRED_LITERALS = " not in code, "a local copy will drift"


@pytest.mark.parametrize("fn", ["_floors_in", "_acceptable_floor"])
def test_the_wrappers_only_delegate(fn):
    """A wrapper that re-implements anything is a second rule wearing the first
    one's name."""
    tree = ast.parse(TEXT)
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == fn)
    body = [st for st in node.body if not isinstance(st, ast.Expr)]
    assert len(body) == 1 and isinstance(body[0], ast.Return), \
        f"{fn} does more than delegate"
    src = ast.get_source_segment(TEXT, body[0])
    assert "_shared_" in src, f"{fn} does not call the shared rule"


def test_the_local_names_still_exist_for_this_modules_lanes():
    """Delegation must not break the callers inside this shell."""
    for caller in ("_floors_in(body, canon)", "_acceptable_floor(body, canon)"):
        assert caller in TEXT, f"{caller} — lane wiring changed unexpectedly"
