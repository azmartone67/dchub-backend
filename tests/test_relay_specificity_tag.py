"""r-continuation (2026-09-03) — the human-line variant must be recorded.

The gated MCP response's one human line was fixed prose and is now quantified
wherever the gate measured something. That change rests on a claim that can be
wrong: that the line failed for want of CONTENT, not placement. Placement was
already measured in both positions — 5,704 paywall signals -> 1 real handoff
open -> 0 converted (r-data-first, 2026-08-26) — so content is the remaining
variable.

If the variant is not recorded, the claim can never be settled. These tests pin
the two properties that make it settle-able:

  1. a KNOWN variant is recorded, with the `trial_preview` prefix preserved so
     every existing rollup and LIKE keeps matching;
  2. an UNKNOWN or missing variant changes nothing — a telemetry field that
     guesses is worse than one that is absent, because the guess gets counted.

Pure-function test: tests/ never imports Flask or the DB, which is exactly why
the logic lives at module scope instead of inside the route handler.
"""
import pytest

from relay_specificity import tag_relay_specificity


@pytest.mark.parametrize("spec", [
    "treatment", "control", "ineligible",   # randomized arms (r-arms)
    "quantified", "generic",                # pre-randomization, shape-assigned
])
def test_known_variant_is_appended_and_prefix_survives(spec):
    out = tag_relay_specificity("trial_preview", spec)
    assert out == "trial_preview:" + spec
    # Every existing rollup keys off this prefix; breaking it would silently
    # empty dashboards rather than fail anything.
    assert out.startswith("trial_preview")


def test_case_and_whitespace_are_normalized():
    assert tag_relay_specificity("trial_preview", "  QUANTIFIED ") == "trial_preview:quantified"


@pytest.mark.parametrize("spec", [None, "", "  ", "maybe", "1", 7, [], {"a": 1}, True])
def test_unknown_or_missing_variant_changes_nothing(spec):
    # ★ The control. An invented label is worse than a missing one: it is
    # indistinguishable from a measured one once it is in the column.
    assert tag_relay_specificity("trial_preview", spec) == "trial_preview"


@pytest.mark.parametrize("msg", [None, "", 0])
def test_no_message_means_no_tag(msg):
    assert tag_relay_specificity(msg, "quantified") == msg


def test_result_is_capped_so_a_long_message_cannot_overflow_the_column():
    long_msg = "x" * 2000
    out = tag_relay_specificity(long_msg, "quantified", _max=2000)
    assert len(out) == 2000


def test_every_arm_the_gate_emits_is_recorded():
    """★ REGRESSION. This nearly failed silently and completely.

    dchub-mcp-server#318 renamed the arms to treatment/control/ineligible when
    assignment became randomized, and for a moment only the READER knew the new
    vocabulary. This function is the WRITER: an unrecognised label is discarded
    and the row is written as a bare `trial_preview`, so the arm would never
    have been recorded at all — the reader perfectly correct, with nothing to
    read, and no error anywhere.

    The list below is the contract. A label the gate emits and this function
    does not know is data loss, not a mismatch.
    """
    for arm in ("treatment", "control", "ineligible"):
        assert tag_relay_specificity("trial_preview", arm) == "trial_preview:" + arm


def test_an_arm_that_does_not_exist_is_still_refused():
    # Widening the vocabulary must not turn this into a pass-through: an
    # invented label in the column is indistinguishable from a measured one.
    for junk in ("treatmentt", "arm", "true", "control ", "  "):
        got = tag_relay_specificity("trial_preview", junk)
        assert got in ("trial_preview", "trial_preview:control"), got
