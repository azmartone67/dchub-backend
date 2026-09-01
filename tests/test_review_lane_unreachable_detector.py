"""radar: the escape valve for refused proposals must admit something.

The review lane was unreachable by construction for its entire life.
`is_unclassified_safe` required `blocked_by` to equal EXACTLY
`["no allowlist transform class matched"]`, and no live row ever did: of the 82
open proposals citing that blocker on 2026-08-31, every one also tripped a
second gate (47 had two, 24 had three, 3 had six).

The tick reported `review_prs_opened: 0` — indistinguishable from "nothing was
eligible today". A flattering zero, and the board read `idle` for 666 runs.

This detector watches the INVARIANT rather than the incident, because a
retuned blocker string or one added gate can re-close the valve silently.
"""

import sys
import types

import pytest

import routes.brain_consistency_radar as radar


@pytest.fixture
def lane(monkeypatch):
    """Install stub autonomy_loop + review_lane modules the detector imports."""
    state = {"rows": [], "err": None, "eligible": set(), "raise_on": None}

    loop = types.ModuleType("routes.brain_autonomy_loop")
    loop._open_proposal_rows = lambda: (state["rows"], state["err"])

    rl = types.ModuleType("routes.brain_review_lane")
    rl._classify = lambda row: row["verdict"]

    def _eligible(verdict):
        if state["raise_on"] == "eligible":
            raise RuntimeError("boom")
        return verdict.get("tag") in state["eligible"]

    rl.is_review_eligible = _eligible
    monkeypatch.setitem(sys.modules, "routes.brain_autonomy_loop", loop)
    monkeypatch.setitem(sys.modules, "routes.brain_review_lane", rl)
    monkeypatch.setattr(radar, "_REVIEW_LANE_REFUSED_FLOOR", 10, raising=False)
    return state


def _refused(tag):
    return {"verdict": {"is_mechanical": False, "tag": tag}}


def _mech():
    return {"verdict": {"is_mechanical": True, "tag": "ok"}}


# ── the incident ─────────────────────────────────────────────────────

def test_the_2026_08_31_shape_fires(lane):
    """★ 82 refused, 0 eligible — the exact state that read as `idle`."""
    lane["rows"] = [_refused(f"p{i}") for i in range(82)]
    lane["eligible"] = set()
    out = radar.check_review_lane_unreachable()
    assert len(out) == 1
    assert out[0]["issue"] == "review_lane_unreachable"
    assert out[0]["count"] == 82
    assert "escape valve admits nothing" in out[0]["detail"]


def test_a_valve_that_admits_even_one_is_silent(lane):
    """The lane working at a trickle is not this defect. The detector must not
    become a general 'throughput is low' alarm — that is a different question
    and would train people to ignore it."""
    lane["rows"] = [_refused(f"p{i}") for i in range(82)]
    lane["eligible"] = {"p7"}
    assert radar.check_review_lane_unreachable() == []


# ── zero is not always a defect ──────────────────────────────────────

def test_an_empty_backlog_is_not_a_stuck_valve(lane):
    """Nothing refused means nothing to admit. Firing here would make the
    detector red on a healthy quiet day and it would be muted within a week."""
    lane["rows"] = []
    assert radar.check_review_lane_unreachable() == []


def test_an_all_mechanical_backlog_is_not_a_stuck_valve(lane):
    """Mechanical proposals leave via the autofix lane; they are not refused."""
    lane["rows"] = [_mech() for _ in range(40)]
    assert radar.check_review_lane_unreachable() == []


def test_a_backlog_under_the_floor_is_silent(lane):
    """Small refused counts are noise — the valve may legitimately reject a
    handful. The floor is what makes this reportable rather than chatty."""
    lane["rows"] = [_refused(f"p{i}") for i in range(5)]
    assert radar.check_review_lane_unreachable() == []


def test_the_floor_is_a_boundary_not_a_range(lane):
    lane["rows"] = [_refused(f"p{i}") for i in range(10)]
    assert radar.check_review_lane_unreachable() == [], "10 is not > 10"
    lane["rows"] = [_refused(f"p{i}") for i in range(11)]
    assert radar.check_review_lane_unreachable(), "11 must fire"


# ── unmeasured is never a pass ───────────────────────────────────────

def test_a_read_error_is_unmeasured_not_clean(lane):
    """★ The empty-range rule. Returning [] on a failed read publishes a
    reassuring green built on no observation — the exact class of bug this
    whole detector exists to catch."""
    lane["err"] = "connection refused"
    out = radar.check_review_lane_unreachable()
    assert len(out) == 1 and out[0]["issue"] == "review_lane_unmeasured"
    assert "not zero" in out[0]["detail"].lower()


def test_a_rename_is_unmeasured_not_clean(monkeypatch):
    """If the lane is renamed the import fails. That must read as UNMEASURED,
    so a refactor cannot silently retire the detector."""
    bad = types.ModuleType("routes.brain_review_lane")   # no is_review_eligible
    monkeypatch.setitem(sys.modules, "routes.brain_review_lane", bad)
    out = radar.check_review_lane_unreachable()
    assert len(out) == 1 and out[0]["issue"] == "review_lane_unmeasured"
    assert "same PR" in out[0]["detail"]


def test_an_eligibility_error_does_not_count_as_eligible(lane):
    """A raising predicate must not be read as 'admitted'. Counting it would
    make an exception storm look like a working valve."""
    lane["rows"] = [_refused(f"p{i}") for i in range(30)]
    lane["raise_on"] = "eligible"
    assert radar.check_review_lane_unreachable(), \
        "an exception is not an admission"


# ── it is actually wired in ──────────────────────────────────────────

def test_the_detector_is_registered():
    """A check that is not in the tuple never runs, and a name in a comment
    does not count — the registry says so in its own comment."""
    import ast
    import pathlib
    src = pathlib.Path(radar.__file__).read_text()
    names = {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
    assert "check_review_lane_unreachable" in names
    code = "\n".join(ln.split("#")[0] for ln in src.split("\n"))
    assert "check_review_lane_unreachable," in code, \
        "registered only in a comment — it would never run"
