"""Approve must act on an INSTRUCTION, and a failed draft must still leave a
trackable artifact.

WHY THIS EXISTS
---------------
2026-09-01. Four investigations were approved on the innovation dashboard and
produced nothing at all — `prs_today: 0`, no code PR, no spec PR. Two
independent causes, one per helper here.

★ 1. THE ITEMS ARE MENUS, NOT INSTRUCTIONS. Measured on the four:

     100419  "Choose the remediation path: (A) ... or (B) ..."
     100418  "Choose the remedy tier: (a) minimal ... or (b) durable ..."
     100417  "Approve (a) ... OR direct a deeper investigation ..."
     100416  "Decide which ... is authoritative, then approve: ..."

  Approve recorded a "yes" to the menu without recording WHICH BRANCH. The
  Layer-5 drafter refuses anything that is not an exact single-file
  substitution — correctly, it cannot pick the operator's branch — so every one
  of those approvals was a no-op by construction. `_resolve_directive` lets the
  operator's own words be what gets drafted.

★ 2. A FAILED DRAFT SKIPPED THE FALLBACK TOO. The spec-PR fallback was gated on
  `pr.get("acted") is False`. That is True for an explicit refusal
  (`{ok:True, acted:False, refused:True}`) but False for a FAILURE, because
  `draft_and_open_pr` returns `{ok:False, error:"claude call failed: http_429"}`
  with no `acted` key — and `None is False` is False. So during the gateway
  spend outage the approval produced neither PR and said nothing.

Both helpers are sliced out of the SHIPPED source with `ast` and executed — no
Flask, no DB, no network.

Run:  python3 -m pytest tests/test_innovation_approve_directive.py -v
"""

import ast
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SRC = _ROOT / "routes" / "brain_innovation_dashboard.py"

# The real text, verbatim from the dashboard on 2026-09-01.
_MENU_100418 = ("Choose the remedy tier: (a) minimal — delete the 19,700 slot and "
                "keep 20,100 as a single conservative floor, adding '19,700' to "
                "stale_markers; or (b) durable — wire both slots to the canonical "
                "facilities getter (the #1390 pattern)")
_OPERATOR = ("Do (a): delete the 19,700 slot from the homepage and add '19,700' "
             "to stale_markers.")


def _load(name: str):
    src = _SRC.read_text()
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            seg = ast.get_source_segment(src, node)
            assert seg and seg.strip(), f"{name} extracted empty"
            ns: dict = {}
            exec(compile(seg, str(_SRC), "exec"), ns)  # noqa: S102
            assert ns[name].__code__.co_code, f"{name} compiled to empty code"
            return ns[name]
    raise AssertionError(f"{name} not found in {_SRC}")


resolve = _load("_resolve_directive")
should_spec = _load("_should_file_spec_pr")


# ── 1. the operator's branch is what gets drafted ────────────────────────────
def test_operator_words_beat_the_menu():
    text, src = resolve(_OPERATOR, _MENU_100418)
    assert text == _OPERATOR and src == "operator"


def test_blank_operator_falls_back_to_the_item():
    """Backward compatible: items that already read as an instruction are
    unchanged."""
    text, src = resolve("", "Repoint routes/foo.py:211 at the canonical getter.")
    assert text == "Repoint routes/foo.py:211 at the canonical getter."
    assert src == "item"


@pytest.mark.parametrize("op", ["", "   ", "\n\t ", None])
def test_whitespace_only_operator_is_not_a_directive(op):
    text, src = resolve(op, "item text")
    assert (text, src) == ("item text", "item")


def test_nothing_anywhere_is_reported_as_none():
    assert resolve("", "") == ("", "none")
    assert resolve(None, None) == ("", "none")


def test_the_operator_can_direct_an_item_that_has_no_text_of_its_own():
    text, src = resolve(_OPERATOR, "")
    assert text == _OPERATOR and src == "operator"


def test_surrounding_whitespace_is_stripped():
    assert resolve("  do the thing  ", "x") == ("do the thing", "operator")


# ── 2. a failed draft still files a spec PR ──────────────────────────────────
def test_the_429_shape_now_files_a_spec_pr():
    """★ The exact regression: the shape draft_and_open_pr returns when the
    Claude call fails. It has NO `acted` key, so `is False` missed it."""
    failed = {"ok": False, "error": "claude call failed: http_429"}
    assert failed.get("acted") is not False, "guard the premise: there is no acted key"
    assert should_spec(failed) is True


def test_an_explicit_refusal_still_files_a_spec_pr():
    assert should_spec({"ok": True, "acted": False, "rationale": "not a single edit",
                        "refused": True}) is True


def test_a_real_pr_does_not_get_a_duplicate_spec_pr():
    assert should_spec({"ok": True, "acted": True, "pr": {"pr_url": "..."}}) is False


def test_gate_closed_also_files_the_approval_somewhere():
    assert should_spec({"ok": False, "error": "autonomy_gate_closed",
                        "reason": "cap"}) is True


@pytest.mark.parametrize("bad", [None, "", [], 0, "acted"])
def test_a_non_dict_is_never_treated_as_a_draft_result(bad):
    assert should_spec(bad) is False


def test_the_old_condition_would_have_missed_the_outage():
    """Regression control — if this ever passes with the NEW condition, the
    scenario stopped reproducing and the tests above prove nothing."""
    failed = {"ok": False, "error": "claude call failed: http_429"}
    old_condition = failed.get("acted") is False       # the shipped bug
    assert old_condition is False, "the old gate skipped the fallback"
    assert should_spec(failed) is True, "the new gate catches it"
