"""The surface-truth retired-floor ban must be canon-relative, not a range.

THE BUG THIS PINS
-----------------
`_STALE_FLOOR` was `\\b(?:12,650|(?:19|20|21|22|23),\\d{3})\\+` — every floor from
19,000 to 23,999 declared retired. PINNED is 18,500+, so `_acceptable_floor`'s
band is [18,500, 20,350]. The live healed floor **19,700+** sits inside the
accept band AND inside the ban at the same time, so every surface simultaneously
reported:

    llms_txt_canon  PASS  "found 19,700+"
    llms_txt_stale  FAIL  "serves retired floor(s): 19,700+"

Three of four lanes permanently red on a contradiction about the identical
bytes — which is exactly why those reds never converted into a fix (SH52-036
records the symptom without the cause).

SECOND OCCURRENCE OF THE CLASS. scripts/accuracy_fence.py froze dchub-frontend
production for 19 consecutive deploys on 2026-08-29 when `[2-9],\\d{3} deals`
matched canon the hour deals_tracked passed 2,000; its facilities twin was ~3
days from the identical freeze. A retired LITERAL stays wrong forever; a retired
RANGE does not — the fleet grows into it. Bans were made canon-relative there.
This shell never got the same treatment, so it rotted the same way.

These tests fail on any return to a hardcoded range.
"""

import ast
import pathlib
import re

import pytest

SRC = (pathlib.Path(__file__).resolve().parents[1]
       / "routes" / "surface_truth_master_shell.py")
TEXT = SRC.read_text()
TREE = ast.parse(TEXT)


def _fn(name):
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _module_const(name):
    """Read a module-level constant from the SOURCE, not a stub.

    ★ The first version of this harness hardcoded _OVERCLAIM_MAX_MULT = 3.0 in
    the exec namespace. That made two mutations survive: widening the bound to
    30x (which re-flags 182,000+ power units) and narrowing it to 1.15x (which
    lets real 21k over-claims escape) both left the suite green, because the
    tests could not see the real constant at all. A guard that supplies its own
    copy of the value under test is not testing that value."""
    for node in TREE.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in surface_truth_master_shell.py")


def _load():
    ns = {"re": re, "_RETIRED_LITERALS": ("12,650+",),
          "_OVERCLAIM_MAX_MULT": _module_const("_OVERCLAIM_MAX_MULT"),
          "_FLOOR_TOKEN": re.compile(r"\b(\d{1,3}(?:,\d{3})+)\+")}
    mod = ast.Module(body=[_fn("_floors_in")], type_ignores=[])
    exec(compile(mod, str(SRC), "exec"), ns)          # noqa: S102 — the point
    return ns["_floors_in"]


CANON = "18,500+"          # PINNED as of 2026-08-31; ceiling = 20,350


# ── the exact contradiction that caused the outage ───────────────────

def test_the_live_healed_floor_is_not_retired():
    """19,700+ is what every surface actually serves and what the canon check
    accepts. It must not also be reported retired."""
    floors_in = _load()
    body = "DC Hub covers 19,700+ data-center facilities across 170+ countries."
    assert floors_in(body, CANON) == []


@pytest.mark.parametrize("v", ["18,500+", "19,000+", "19,700+", "20,350+"])
def test_nothing_inside_the_acceptance_band_is_ever_retired(v):
    """The two checks must read ONE band. Anything _acceptable_floor takes,
    _floors_in must leave alone."""
    floors_in = _load()
    assert floors_in(f"we track {v} facilities", CANON) == []


def test_the_band_ceiling_matches_acceptable_floor_exactly():
    """Both must use base * 1.10. If they drift apart the contradiction returns
    in the gap between the two ceilings."""
    a = ast.get_source_segment(TEXT, _fn("_acceptable_floor"))
    f = ast.get_source_segment(TEXT, _fn("_floors_in"))
    assert "int(base * 1.10)" in a
    assert "int(base * 1.10)" in f


# ── genuine over-claims are still caught ─────────────────────────────

@pytest.mark.parametrize("v", ["21,000+", "23,000+", "30,000+", "50,000+"])
def test_facility_scale_over_claims_are_still_retired(v):
    """The fence must keep doing its job — this is not a loosening. Every floor
    it has ever had to retire (19k-23k) sits inside the window."""
    floors_in = _load()
    assert floors_in(f"we track {v} facilities", CANON) == [v]


@pytest.mark.parametrize("v,what", [
    ("182,000+", "global power generating UNITS"),
    ("330,000+", "mapped infrastructure assets"),
    ("143,000+", "the ai_discovery_routes emitter figure"),
    ("126,000+", "substations"),
])
def test_other_canon_quantities_are_not_facility_over_claims(v, what):
    """★ THE REGRESSION THIS BOUND EXISTS FOR. Shipped without an upper limit,
    the rule flagged every one of these within an hour of going live — taking
    served_manifests and repo_vs_served red on false grounds and pushing
    emitter_sources from PASS to FAIL.

    "Above the acceptance ceiling" is not "a facility over-claim": the other
    canon quantities live up there and are all legitimate. The old range regex
    missed them only because 19,000-23,999 happened to sit below — the right
    property by accident."""
    floors_in = _load()
    assert floors_in(f"DC Hub maps {v} {what}", CANON) == [], \
        f"{v} is {what}, not a facility floor"


def test_retired_literals_are_still_caught():
    floors_in = _load()
    assert "12,650+" in floors_in("legacy claim of 12,650+ sites", CANON)


def test_the_ban_grows_with_canon_instead_of_rotting():
    """The property the range ban lacked. At canon 18,500+, 21,000+ is an
    over-claim; once canon reaches 21,000+, the same token is canon-family and
    must clear itself with no code edit."""
    floors_in = _load()
    body = "we track 21,000+ facilities"
    assert floors_in(body, "18,500+") == ["21,000+"]
    assert floors_in(body, "21,000+") == []


# ── unknown canon is indeterminate, never clean ──────────────────────

def test_unresolvable_canon_returns_none_not_empty():
    """A fence that cannot resolve canon must not certify a page as clean.
    Returning [] here would read as PASS — the fail-open direction."""
    floors_in = _load()
    assert floors_in("we track 99,000+ facilities", None) is None
    assert floors_in("anything", "not-a-number") is None


def test_indeterminate_reaches_the_checks_as_none_not_false():
    """A None must render '?' at every call site, not a pass and not a fail."""
    body = ast.get_source_segment(TEXT, _fn("_audit_body"))
    assert "(None if stale is None else not stale)" in body
    emitters = TEXT[TEXT.find("def _emitter"):] if "def _emitter" in TEXT else TEXT
    assert "canon floor unresolvable" in TEXT


# ── the range ban must not come back ─────────────────────────────────

def test_no_hardcoded_multi_thousand_range_ban_remains():
    """The literal shape that rotted. Its return would re-create the outage."""
    assert "_STALE_FLOOR" not in TEXT, \
        "the range-based ban is back — a retired RANGE rots as the fleet grows"
    assert not re.search(r"\(19\|20\|21\|22\|23\)", TEXT)


def test_retired_literals_are_literals_not_patterns():
    """Literals are safe to hardcode forever; ranges are not. Enforce that the
    hardcoded list contains only fully-specified tokens."""
    lits = None
    for node in TREE.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "_RETIRED_LITERALS" for t in node.targets):
            lits = ast.literal_eval(node.value)
    assert lits, "_RETIRED_LITERALS missing"
    for lit in lits:
        assert re.fullmatch(r"\d{1,3}(,\d{3})+\+", lit), \
            f"{lit!r} is not a fully-specified literal — ranges rot"


def test_the_over_claim_bound_is_facility_scale():
    """Pin the VALUE, not just its presence. Too wide and the other canon
    quantities get flagged again (the live regression); too tight and the 19k-23k
    floors this fence exists for escape."""
    mult = _module_const("_OVERCLAIM_MAX_MULT")
    base = 18500
    assert base * mult >= 23000 * 1.05, (
        f"{mult}x cannot reach the 19k-23k floors this fence retires")
    assert base * mult < 126000, (
        f"{mult}x reaches substations/power-units/asset counts — the exact "
        f"false-positive that took three lanes red on 2026-08-31")
