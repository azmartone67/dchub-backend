"""Attribute each automerge refusal to the gate that actually owns it.

WHY THIS EXISTS
---------------
The automerge board showed `idle` for 666 consecutive runs, and the brain's own
status text explained it as proposals "rejected `not_mechanical` against a
6-class SQL/datetime allowlist". That reads as one instruction: widen the
allowlist.

Measured against all 85 open proposals on 2026-08-31:

    cited                          would release if removed
      82  no_class                    0  no_class
      64  low_confidence              1  low_confidence
      32  adds_new_call               0
      24  too_many_lines              0
      15  adds_control_flow           0
       3  adds_import                 0

Widening the allowlist releases NOTHING. Every one of the 82 that cite it also
trips another gate — 47 have two blockers, 24 have three, 3 have six. The most
common pair is low_confidence + no_class (34), where the model scored its own
fix at 0.55 against a 0.8 bar.

The defect was never the gate. It was that the most-CITED blocker is never the
DECIDING one, so the board pointed at the only lever that changes nothing. This
module reports both numbers so the next person does not spend a day widening an
allowlist that releases zero proposals — which is exactly what I started to do.
"""

import ast
import pathlib

import pytest

SRC = (pathlib.Path(__file__).resolve().parents[1]
       / "routes" / "brain_mechanical_classifier.py")
TEXT = SRC.read_text()
TREE = ast.parse(TEXT)


def _fn(name):
    for n in TREE.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"{name} not found")


def _load():
    """Exec the two pure helpers against a stubbed classifier."""
    verdicts = {}

    def _classify(row):
        return verdicts[row["id"]]

    ns = {"classify_mechanical": _classify,
          "_BLOCKER_LABELS": tuple(
              ast.literal_eval(
                  ast.get_source_segment(TEXT, _fn_assign("_BLOCKER_LABELS"))))}
    for name in ("_blocker_key", "attribute_blockers"):
        exec(compile(ast.Module(body=[_fn(name)], type_ignores=[]),  # noqa: S102
                     str(SRC), "exec"), ns)
    return ns, verdicts


def _fn_assign(name):
    for n in TREE.body:
        if isinstance(n, ast.Assign) and any(
                getattr(t, "id", "") == name for t in n.targets):
            return n.value
    raise AssertionError(f"{name} not found")


def _blocked(*reasons):
    return {"is_mechanical": False, "blocked_by": list(reasons)}


NO_CLASS = "no allowlist transform class matched"
LOW_CONF = "confidence 0.55 < MECH_MIN_CONF=0.8"
LINES = "10 changed lines > MECH_MAX_LINES=8"


# ── the distinction the board was missing ────────────────────────────

def test_a_gate_that_never_decides_alone_reports_zero():
    """THE point. A proposal blocked by two gates is released by neither."""
    ns, v = _load()
    v[1] = _blocked(NO_CLASS, LOW_CONF)
    v[2] = _blocked(NO_CLASS, LOW_CONF)
    out = ns["attribute_blockers"]([{"id": 1}, {"id": 2}])
    assert out["cited"]["no_class"] == 2
    assert out["cited"]["low_confidence"] == 2
    assert out["would_release_if_removed"].get("no_class", 0) == 0
    assert out["would_release_if_removed"].get("low_confidence", 0) == 0


def test_a_sole_blocker_is_credited():
    ns, v = _load()
    v[1] = _blocked(NO_CLASS)
    v[2] = _blocked(NO_CLASS, LOW_CONF)
    out = ns["attribute_blockers"]([{"id": 1}, {"id": 2}])
    assert out["cited"]["no_class"] == 2
    assert out["would_release_if_removed"]["no_class"] == 1, \
        "only the proposal that gate refuses ALONE counts"


def test_cited_and_sole_are_reported_separately():
    """Collapsing them back into one number recreates the original defect."""
    ns, v = _load()
    v[1] = _blocked(NO_CLASS, LOW_CONF, LINES)
    out = ns["attribute_blockers"]([{"id": 1}])
    assert out["cited"] != out["would_release_if_removed"]
    assert out["would_release_if_removed"] == {}


def test_mechanical_proposals_are_counted_not_blamed():
    ns, v = _load()
    v[1] = {"is_mechanical": True, "klass": "now_text_cast"}
    v[2] = _blocked(NO_CLASS)
    out = ns["attribute_blockers"]([{"id": 1}, {"id": 2}])
    assert out["total"] == 2 and out["mechanical"] == 1 and out["blocked"] == 1
    assert "no_class" in out["cited"]


# ── it never hides a stuck proposal ──────────────────────────────────

def test_a_classifier_error_is_counted_not_dropped():
    """A proposal that cannot be classified is still a proposal that is not
    moving. Dropping it would shrink the backlog on paper."""
    ns, v = _load()

    def _boom(row):
        raise RuntimeError("classifier exploded")

    ns["classify_mechanical"] = _boom
    out = ns["attribute_blockers"]([{"id": 1}])
    assert out["total"] == 1 and out["blocked"] == 1
    assert out["cited"].get("classifier_error") == 1


def test_a_refusal_with_no_stated_reason_is_still_visible():
    ns, v = _load()
    v[1] = {"is_mechanical": False, "blocked_by": []}
    out = ns["attribute_blockers"]([{"id": 1}])
    assert out["cited"].get("unexplained") == 1
    assert out["would_release_if_removed"].get("unexplained") == 1


def test_empty_input_is_not_an_error():
    ns, _ = _load()
    out = ns["attribute_blockers"]([])
    assert out["total"] == 0 and out["blocked"] == 0


# ── gate naming ──────────────────────────────────────────────────────

@pytest.mark.parametrize("reason,key", [
    (NO_CLASS, "no_class"),
    (LOW_CONF, "low_confidence"),
    (LINES, "too_many_lines"),
    ("adds an import", "adds_import"),
    ("adds control-flow keyword(s): except,try", "adds_control_flow"),
    ("adds call name(s) not in search: to_regclass", "adds_new_call"),
    ("something nobody labelled", "other"),
])
def test_every_live_blocker_string_maps_to_a_gate(reason, key):
    """These are the exact strings the classifier emits today. An unmapped one
    lands in 'other', which is visible — never silently dropped."""
    ns, _ = _load()
    assert ns["_blocker_key"](reason) == key


def test_the_note_warns_against_the_wrong_read():
    """The payload has to carry its own instruction — a raw pair of dicts
    invites exactly the misreading this replaces."""
    ns, v = _load()
    v[1] = _blocked(NO_CLASS)
    out = ns["attribute_blockers"]([{"id": 1}])
    assert "sole_blocker" in out["note"]
    assert "releases nothing" in out["note"]


# ── the status endpoint actually serves it ───────────────────────────

def test_status_endpoint_reports_blockers_and_the_review_lane():
    """A diagnostic nobody serves is the failure mode this whole audit kept
    finding."""
    loop = (pathlib.Path(__file__).resolve().parents[1]
            / "routes" / "brain_autonomy_loop.py").read_text()
    assert "attribute_blockers" in loop
    assert "blockers=blockers" in loop
    assert '"review_prs_opened": s.get("review_prs_opened")' in loop, \
        "the review lane is the escape valve for refused work — show it"
    assert "must not 500" in loop, "a diagnostic must never break the status page"
