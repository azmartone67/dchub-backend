"""util/canon_floor — the ONE rule for "does this surface quote the right floor".

Four guards asked that question and answered it four different ways, producing
three separate false REDs on healthy files in a single day (2026-08-31):

    surface_truth          banned the RANGE 19,000-23,999. Once PINNED reached
                           18,500+, the live-healed 19,700+ sat inside both the
                           accept band and the ban — every surface passed
                           "carries canon floor" and failed "free of retired
                           floors" on the same bytes. Three lanes red.
    loop_control           compared against the LIVE count, >500 behind = stale.
                           Live moves; the files carry PINNED. Permanently red.
    seven_levers           a COPY of the rotted regex plus an exact
                           `canon_floor not in body`. A card at 19,900+ was
                           called retired AND missing canon at once.
    intelligence_expansion the same copy, the same exact-match.

These tests import the module rather than parsing its source, so they exercise
the behaviour and not the spelling.
"""

import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from util import canon_floor as cf  # noqa: E402

CANON = "18,500+"          # PINNED on 2026-08-31; accept <= 20,350, retire <= 55,500


# ── the cases that were actually wrong in production ─────────────────

@pytest.mark.parametrize("body,why", [
    ("we track 18,500+ facilities", "PINNED exactly"),
    ("we track 19,700+ facilities", "live-healed — the surface_truth false red"),
    ("we track 19,900+ facilities", "live-healed — the zone_sync card"),
    ("we track 20,350+ facilities", "top of the accept band"),
])
def test_a_canon_family_floor_is_accepted_and_never_retired(body, why):
    """The whole point: a floor rounds DOWN and may lag, so PINNED or any
    live-healed value inside the band is CORRECT. Nothing accepted may also be
    reported retired — that contradiction is what reddened three lanes."""
    assert cf.acceptable_floor(body, CANON) is not None, why
    assert cf.retired_floors(body, CANON) == [], why


@pytest.mark.parametrize("v", ["21,000+", "23,000+", "50,000+"])
def test_facility_scale_over_claims_are_retired(v):
    """Not a loosening — every floor this class ever had to retire (19k-23k
    against a canon in the teens of thousands) is inside the window."""
    assert cf.retired_floors(f"we track {v} facilities", CANON) == [v]


def test_the_retired_literal_is_always_retired():
    """12,650+ was canon itself for four days in July. A LITERAL that is
    permanently wrong is safe to hardcode; a RANGE is not."""
    assert "12,650+" in cf.retired_floors("legacy 12,650+ sites", CANON)


@pytest.mark.parametrize("v,what", [
    ("126,000+", "substations"),
    ("182,000+", "global power generating units"),
    ("330,000+", "mapped infrastructure assets"),
])
def test_other_canon_quantities_are_left_alone(v, what):
    """★ The regression a previous fix caused. "Above the accept ceiling" is not
    "a facility over-claim" — the other canon quantities live up there and are
    all legitimate. Flagging them took served_manifests and repo_vs_served red
    and pushed emitter_sources from PASS to FAIL."""
    assert cf.retired_floors(f"DC Hub maps {v} {what}", CANON) == [], what


def test_a_floor_below_the_pin_is_missing_not_retired():
    """15,000+ against a pin of 18,500+ is a stale surface. It is reported as
    "no canon-family floor", not as a retired over-claim — the two are different
    defects and conflating them is what produced "retired AND absent" on one
    healthy card."""
    body = "we track 15,000+ facilities"
    assert cf.acceptable_floor(body, CANON) is None
    assert cf.retired_floors(body, CANON) == []
    assert cf.floor_verdict(body, CANON)["ok"] is False


# ── it cannot rot ────────────────────────────────────────────────────

def test_both_bounds_move_with_canon():
    """A retired RANGE rots as the fleet grows into it — that is how
    accuracy_fence froze dchub-frontend for 19 deploys when `[2-9],\\d{3} deals`
    matched canon the hour deals_tracked passed 2,000. Both bounds here are
    derived from canon, so the rule self-heals across pin bumps."""
    body = "we track 21,000+ facilities"
    assert cf.retired_floors(body, "18,500+") == ["21,000+"]
    assert cf.retired_floors(body, "21,000+") == []     # canon caught up
    assert cf.acceptable_floor(body, "21,000+") == "21,000+"


def test_no_hardcoded_range_ban_in_the_source():
    src = (pathlib.Path(cf.__file__)).read_text()
    assert "19|20|21|22|23" not in src, \
        "the rotted range ban is back — it rots as the fleet grows"


def test_the_bounds_are_facility_scale():
    """Too wide and other quantities get flagged; too tight and the 19k-23k
    floors this exists to retire escape."""
    base = 18500
    assert base * cf.OVERCLAIM_MAX_MULT >= 23000 * 1.05, "cannot reach 19k-23k"
    assert base * cf.OVERCLAIM_MAX_MULT < 126000, "reaches substations"
    assert 1.0 < cf.ACCEPT_MAX_MULT <= 1.25, "accept band is not a floor band"
    assert cf.ACCEPT_MAX_MULT < cf.OVERCLAIM_MAX_MULT


# ── unknown canon is indeterminate, never clean ──────────────────────

@pytest.mark.parametrize("canon", [None, "", "not-a-number", 0])
def test_unresolvable_canon_returns_none_not_empty(canon):
    """[] would read as PASS. A fence that cannot resolve canon must not
    certify a page — that is the fail-open direction."""
    assert cf.retired_floors("we track 99,000+ facilities", canon) is None
    assert cf.floor_verdict("anything", canon)["ok"] is None


def test_retired_literals_are_literals_not_patterns():
    import re as _re
    assert cf.RETIRED_LITERALS
    for lit in cf.RETIRED_LITERALS:
        assert _re.fullmatch(r"\d{1,3}(,\d{3})+\+", lit), \
            f"{lit!r} is not a fully-specified literal — ranges rot"


# ── every shell actually uses it ─────────────────────────────────────

@pytest.mark.parametrize("rel", [
    "routes/surface_truth_master_shell.py",
    "routes/seven_levers_master_shell.py",
    "routes/intelligence_expansion_master_shell.py",
])
def test_the_shells_delegate_rather_than_carry_a_copy(rel):
    """A shared rule nobody imports is a fourth copy. And the copies are what
    made four guards disagree in the first place."""
    src = (pathlib.Path(__file__).resolve().parents[1] / rel).read_text()
    assert "from util.canon_floor import" in src, f"{rel} does not use the rule"
    code = "\n".join(l.split("#")[0] for l in src.split("\n"))
    assert "19|20|21|22|23" not in code, f"{rel} still carries a range ban"
    assert "canon_floor not in body" not in code, \
        f"{rel} still exact-matches the floor — a live-healed surface fails it"


# ── the module is exempt from the stale-count scan; earn it ──────────

def test_no_count_literal_escapes_the_denylist():
    """★ canon_floor.py is in STALE_SCAN_SKIP_FILES because a denylist must be
    allowed to name what it bans — the same reason ai_surface_canon.py is
    exempt. A blanket file skip is the fail-open direction, so this stands in
    for the scan and is stricter than it: EVERY comma-formatted count in the
    module's code must be inside RETIRED_LITERALS. A stale floor added anywhere
    else in this file fails here instead of passing unscanned."""
    import ast
    import re as _re
    src = pathlib.Path(cf.__file__).read_text()
    tree = ast.parse(src)

    allowed = set(cf.RETIRED_LITERALS)
    # Docstrings are prose and out of scope, exactly as they are for the scan
    # this replaces ("None is an emitted string literal" — the note in
    # test_canonical_counts_drift.py). This module's docstring narrates the
    # 2026-08-31 incident and necessarily quotes the floors involved.
    exempt = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                exempt.add(id(body[0].value))
    # The token regex itself is the one string that must contain digit-group
    # syntax without being a count. Identify it by node, not by spelling.
    pattern_nodes = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(getattr(x, "id", "") == "FLOOR_TOKEN"
                        for x in node.targets)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    pattern_nodes.add(id(sub))

    count = _re.compile(r"\b\d{1,3}(?:,\d{3})+\+?")
    offenders = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in pattern_nodes
                and id(node) not in exempt):
            for hit in count.findall(node.value):
                if hit not in allowed:
                    offenders.append(f"line {node.lineno}: {hit!r}")
    assert not offenders, (
        "count literal(s) outside RETIRED_LITERALS in a file the stale-count "
        "scan skips: " + "; ".join(offenders))
