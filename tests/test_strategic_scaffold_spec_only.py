"""The strategic scaffold's SPEC-ONLY marker — the gate contract, not a substring.

★ THE TRAP THIS CLOSES. `brain-pr-substance-gate` classifies a diff of only
`docs/**` + `routes/_proposed_*` as INERT, then greps the PR title+body for
close/fix/resolve and HARD-FAILS the check on a hit, because a scaffold that
claims a fix is the false-resolution pattern the gate exists to stop. The
brain's strategic drafts are inert by construction — but their `### Why`
section is LLM prose the brain wrote ABOUT WHAT TO BUILD, so "the epistemics
fix", "not because fixes fail" and "resolve" land in it by accident.

19 of the 60 most recent `brain-l6/strategic-*` PRs tripped that regex and
blocked on a required check. The other 41 passed only because their prose
happened to dodge eight words. `brain_pr_opener` fixed this for its own path on
2026-07-18 with the SPEC-ONLY marker (r-spec-honesty); `brain_strategic_planner`
is the SECOND PR-opening path and never adopted it.

★ WHY THESE TESTS ARE NOT SUBSTRING CHECKS. The gate's regex and marker string
are read out of `.github/workflows/brain-pr-substance-gate.yml` AT TEST TIME.
A copied regex would drift silently the moment the gate is retuned and these
tests would keep passing against a rule that no longer exists.

★ THE VACUITY CONTROL. `test_the_spec_prose_really_does_trip_the_regex` asserts
the fixture prose HITS the fix-claim regex. Without it, the exemption tests
would pass just as well on prose that was never going to block — green because
nothing was being guarded. See feedback_mutation_masked_by_earlier_guard.
"""
import pathlib
import re

import pytest

import routes.brain_strategic_planner as planner

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_GATE = _ROOT / ".github/workflows/brain-pr-substance-gate.yml"

# The real spec text of be#3418, which blocked. "fix" appears twice, both times
# as the brain describing a thing to build — never as a claim about this PR.
_SPEC_3418 = (
    "145 merged PRs in 30 days grade 'unknown' with before=null/after=null; "
    "success_rate reads 0.0 not because fixes fail but because no baseline is "
    "ever captured. Without this, every other item in this synthesis is "
    "unfalsifiable — this is the epistemics fix that makes the brain honest."
)

_INERT_FILES = ["docs/strategic/x.md", "routes/_proposed_x.py"]


def _gate_source() -> str:
    assert _GATE.exists(), f"substance gate workflow missing at {_GATE}"
    return _GATE.read_text()


def _gate_fix_regex() -> str:
    """The gate's OWN fix-claim regex, lifted from the workflow."""
    m = re.search(r"grep -qiE '([^']+)'", _gate_source())
    assert m, "could not locate the fix-claim regex in the substance gate"
    return m.group(1)


def _gate_marker() -> str:
    """The exact string the gate greps for to exempt an honest scaffold."""
    m = re.search(r"grep -q '([A-Z-]+)'", _gate_source())
    assert m, "could not locate the SPEC-ONLY marker grep in the substance gate"
    return m.group(1)


def _gate_verdict(title: str, body: str, files) -> str:
    """Reproduce the gate: 'failure' | 'neutral' | 'pass'.

    Mirrors the workflow's branch order exactly — inert first, then fix-claim,
    then the marker override.
    """
    inert = all(
        f.startswith("docs/")
        or f.startswith("routes/_proposed_")
        or f == ".github/workflows/brain-pr-substance-gate.yml"
        for f in files
    )
    if not inert:
        return "pass"
    claims_fix = bool(re.search(_gate_fix_regex(), f"{body} {title}", re.I))
    if _gate_marker() in body:
        claims_fix = False
    return "failure" if claims_fix else "neutral"


def _body(spec=_SPEC_3418) -> str:
    return planner._scaffold_pr_body(
        "strategic_gap_4w", "2026-08-31", 0.85, "_(not quantified)_",
        spec, "docs/strategic/x.md", "routes/_proposed_x.py", "- `evidence=1`")


# ---------------------------------------------------------------- vacuity control

def test_the_spec_prose_really_does_trip_the_regex():
    """Without this, every exemption test below could pass vacuously."""
    assert re.search(_gate_fix_regex(), _SPEC_3418, re.I), (
        "fixture spec no longer contains a fix-word — the exemption tests "
        "would be guarding nothing")


def test_the_gate_workflow_still_greps_for_the_marker_we_emit():
    assert _gate_marker() == planner.SPEC_ONLY_MARKER


# ---------------------------------------------------------------- the fix

def test_a_strategic_scaffold_with_fix_prose_is_neutral_not_blocked():
    assert _gate_verdict("[brain-l6 strategic-draft] x", _body(),
                         _INERT_FILES) == "neutral"


def test_every_generated_body_leads_with_the_marker():
    first = _body().split("\n")[0]
    assert planner.SPEC_ONLY_MARKER in first, (
        "marker must lead the body so it survives body[:4000] truncation")


@pytest.mark.parametrize("spec", [
    "", None, "closes the loop", "resolved by fixing the resolver", _SPEC_3418,
])
def test_no_spec_prose_can_block_a_scaffold(spec):
    assert _gate_verdict("[brain-l6 strategic-draft] x", _body(spec),
                         _INERT_FILES) == "neutral"


# ---------------------------------------------------------------- mutation controls

def test_without_the_marker_the_same_body_blocks():
    """RED/GREEN pair: proves the marker is what flips the verdict."""
    stripped = _body().replace(planner.SPEC_ONLY_MARKER, "")
    assert _gate_verdict("[brain-l6 strategic-draft] x", stripped,
                         _INERT_FILES) == "failure"


def test_the_marker_does_not_exempt_a_pr_that_touches_running_code():
    """The marker must not become a blanket bypass."""
    assert _gate_verdict("[brain-l6 strategic-draft] x", _body(),
                         _INERT_FILES + ["main.py"]) == "pass"
