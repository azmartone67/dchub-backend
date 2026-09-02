"""The backlog verdict may never publish a blocker it has not measured.

★ THE STATE THIS EXISTS FOR, live on /api/v1/brain/status until 2026-09-02:

    verdict:        healthy_backlog
    verdict_detail: "... ★ Verify that routing rather than assuming it:
                     measured 2026-08-30, brain-autonomy evaluated 22
                     proposals and opened 0 — 21 `not_mechanical` against a
                     6-class SQL/datetime allowlist. A backlog that routes
                     into a filter which rejects everything is not routed,
                     it is parked."

That sentence was a HARDCODED measurement from 2026-08-30 that named the
allowlist as the lever to pull. On 2026-08-31 —
brain_mechanical_classifier.attribute_blockers() — it was measured and
REFUTED:

    82  of 85 open proposals cite "no allowlist transform class matched"
     0  would become mechanical if that gate were removed

Every one of the 82 carried another blocker (34 were low_confidence +
no_class: the model scoring its own fix at 0.55 against a 0.8 bar). Widening
the allowlist releases NOTHING; it moves the refusal one gate along.

★ THE FROZEN SENTENCE OUTLIVED THE MEASUREMENT THAT REFUTED IT, and kept
being served live. Readers — this repo's own sessions among them — went on
proposing the one fix that changes nothing, because the status endpoint told
them to. The defect is not the allowlist and never was: it is a status
surface publishing a conclusion it did not derive.

★ THE INVARIANT. The verdict may name a blocker gate ONLY when live
attribution shows that gate SOLELY owns at least one refusal. `cited` is not
`sole_blocker`, and the most-cited gate is characteristically NOT the
deciding one — that is the whole bug. With no attribution, or with every
refusal held by two or more gates, the verdict must name NO gate.

Behavioural: every assertion below drives compute_brain_verdict and reads
the string it actually returns. Nothing here greps source text — a guard
that greps would have passed on the frozen sentence for as long as the
sentence sat there, which is precisely how this survived.
"""
from routes.brain_v2_layer4 import compute_brain_verdict

FRESH_LOG = 129
RECENT_RUN = 21

# Every gate name the classifier can attribute a refusal to. The verdict is
# forbidden from naming any of these without live evidence for that one.
GATE_NAMES = ("no_class", "low_confidence", "too_many_lines",
              "adds_import", "adds_control_flow", "adds_new_call")

# The refuted claim's fingerprints. Prose may be reworded; these are the
# load-bearing assertions that were wrong.
REFUTED_FINGERPRINTS = ("6-class", "not_mechanical", "SQL/datetime",
                        "measured 2026-08-30")


def _detail(actionable=22, attribution=None):
    verdict, detail = compute_brain_verdict(
        True, RECENT_RUN, FRESH_LOG, 0, 7424,
        actionable_count=actionable, blocker_attribution=attribution)
    assert verdict == "healthy_backlog", f"expected backlog, got {verdict}"
    return detail


def test_without_attribution_it_names_no_gate_at_all():
    """Production takes this path today: nothing measured, so nothing named."""
    d = _detail()
    named = [g for g in GATE_NAMES if g in d]
    assert not named, f"named {named} with no attribution to justify it"


def test_without_attribution_it_points_at_the_instrument():
    """Refusing to guess is only useful if it says where the answer lives."""
    assert "sole_blocker" in _detail()


def test_the_refuted_frozen_measurement_is_gone():
    """The exact claim that was measured and disproved must not be served."""
    d = _detail()
    for fp in REFUTED_FINGERPRINTS:
        assert fp not in d, f"refuted claim still published: {fp!r}"


def test_it_names_the_deciding_gate_when_one_owns_a_refusal_alone():
    """★ THE GREEN DIRECTION. Without this, deleting the sentence entirely
    would pass every other test in this file — an unconditionally-silent
    verdict is not a fix."""
    d = _detail(attribution={"blocked": 85,
                             "cited": {"no_class": 82, "low_confidence": 34},
                             "sole_blocker": {"low_confidence": 34}})
    assert "low_confidence" in d, "the deciding gate must be named"
    assert "34" in d, "and with the count that makes it decidable"


def test_the_most_cited_gate_is_not_named_when_it_decides_nothing():
    """★ THE ACTUAL BUG, encoded. no_class is cited 82 times and owns ZERO
    refusals alone. Naming it is what sent every reader at the dead lever."""
    d = _detail(attribution={"blocked": 85,
                             "cited": {"no_class": 82, "low_confidence": 34},
                             "sole_blocker": {"low_confidence": 34}})
    assert "no_class" not in d, "named the most-cited gate, which releases nothing"


def test_when_no_gate_owns_a_refusal_alone_it_says_so_and_names_none():
    """The measured 08-31 shape: 82 blocked, every one held by 2+ gates."""
    d = _detail(attribution={"blocked": 82, "cited": {"no_class": 82},
                             "sole_blocker": {}})
    named = [g for g in GATE_NAMES if g in d]
    assert not named, f"named {named} when no gate decides anything"
    assert "82" in d
    assert "releases nothing" in d


def test_a_zero_valued_sole_blocker_is_not_a_deciding_gate():
    """A gate present in the dict with count 0 must not be named — the dict
    carrying the key is not evidence the gate holds anything."""
    d = _detail(attribution={"blocked": 5, "cited": {"adds_import": 5},
                             "sole_blocker": {"adds_import": 0}})
    assert "adds_import" not in d


def test_malformed_attribution_degrades_to_naming_nothing():
    """A classifier error must never be able to invent a culprit."""
    for junk in (None, "sole_blocker", [], 42, {}):
        d = _detail(attribution=junk)
        named = [g for g in GATE_NAMES if g in d]
        assert not named, f"attribution={junk!r} produced a named gate: {named}"
