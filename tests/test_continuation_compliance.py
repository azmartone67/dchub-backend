"""Did the AGENT act on the continuation? — the funnel's eighth row.

The instrumentation table for this funnel had seven answerable rows and one
that was called unmeasurable: "did the agent surface the continuation at all?"
It IS measurable, because the continuation instruction is a tool call — an
agent that preserved it calls unlock_more_data / claim_free_key / bind_email in
the same session, after the gate.

These tests pin the properties that make the answer trustworthy rather than
merely present:

  1. a rate over a ZERO denominator is UNMEASURED, never 0%. This feature
     deployed the same day it was written, so "no tagged signals yet" and
     "agents ignore us" produce the same 0 and mean opposite things — the one
     confusion that would waste the most time downstream;
  2. rows written before the tagging existed land in their own `untagged`
     bucket, never folded into the control arm, which would silently move
     whichever rate they were folded into;
  3. the two arms are only compared when BOTH have a real denominator.

Pure-function test: tests/ never imports Flask or the DB, which is why the
logic lives in its own import-free module rather than in the route handler.
"""
import pytest

from continuation_compliance import (
    CONTINUATION_TOOLS, parse_arm, summarize_compliance,
)


# ── the arm label ────────────────────────────────────────────────────────
@pytest.mark.parametrize("shown,expected", [
    ("trial_preview:quantified", "quantified"),
    ("trial_preview:generic",    "generic"),
    ("trial_preview:QUANTIFIED", "quantified"),   # normalized upstream, belt and braces
    ("trial_preview",            "untagged"),     # pre-tagging rows
    ("trial_preview:maybe",      "untagged"),     # an arm we never emit
    ("",                         "untagged"),
    (None,                       "untagged"),
    (17,                         "untagged"),
])
def test_arm_is_parsed_or_bucketed_as_untagged(shown, expected):
    assert parse_arm(shown) == expected


def test_untagged_rows_are_never_folded_into_the_control_arm():
    # ★ Pre-tagging history is large and its arm is unknowable. Counting it as
    # `generic` would move the control rate by an unknown amount in an unknown
    # direction, and the comparison would look fine while being wrong.
    out = summarize_compliance([("trial_preview", 900, 3)])
    assert out["arms"]["untagged"]["gated_sessions"] == 900
    assert out["arms"]["generic"]["state"] == "UNMEASURED"
    assert out["arms"]["quantified"]["state"] == "UNMEASURED"


# ── the zero-denominator rule ────────────────────────────────────────────
def test_empty_window_is_unmeasured_not_zero_percent():
    # ★ THE CONTROL. The failure this whole module is shaped around.
    out = summarize_compliance([])
    for arm in ("quantified", "generic", "untagged"):
        assert out["arms"][arm]["state"] == "UNMEASURED"
        assert "acted_rate" not in out["arms"][arm]
    assert out["totals"]["state"] == "UNMEASURED"
    assert out["comparison"]["state"] == "UNMEASURED"


def test_zero_acted_on_a_real_denominator_IS_measured():
    # The mirror image: 0 of 400 is a real, alarming finding — not an absence.
    out = summarize_compliance([("trial_preview:quantified", 400, 0)])
    q = out["arms"]["quantified"]
    assert q["state"] == "MEASURED"
    assert q["acted_rate"] == 0.0


# ── arithmetic that cannot flatter itself ────────────────────────────────
def test_acted_can_never_exceed_gated():
    out = summarize_compliance([("trial_preview:generic", 10, 99)])
    g = out["arms"]["generic"]
    assert g["acted_sessions"] == 10 and g["acted_rate"] == 1.0


@pytest.mark.parametrize("row", [
    ("trial_preview:generic", "x", 1), ("trial_preview:generic", -5, 1),
    ("trial_preview:generic", 5, -1), (), None, ("only-one-field",),
])
def test_malformed_rows_are_dropped_not_guessed(row):
    assert summarize_compliance([row])["totals"]["state"] == "UNMEASURED"


def test_multiple_rows_in_one_arm_accumulate():
    out = summarize_compliance([
        ("trial_preview:quantified", 100, 10),
        ("trial_preview:quantified", 100, 20),
    ])
    assert out["arms"]["quantified"]["gated_sessions"] == 200
    assert out["arms"]["quantified"]["acted_rate"] == 0.15


# ── the comparison ───────────────────────────────────────────────────────
def test_comparison_needs_both_arms_populated():
    out = summarize_compliance([("trial_preview:quantified", 400, 52)])
    assert out["comparison"]["state"] == "UNMEASURED"


def test_comparison_reports_the_raw_difference_and_says_it_is_raw():
    out = summarize_compliance([
        ("trial_preview:quantified", 400, 52),
        ("trial_preview:generic",    400, 40),
    ])
    c = out["comparison"]
    assert c["state"] == "MEASURED"
    assert c["difference"] == pytest.approx(0.03, abs=1e-6)
    # It must not pass itself off as a significance test.
    assert "not a significance test" in c["note"]


def test_the_tools_counted_as_compliance_are_published_in_the_response():
    # A consumer cannot interpret the rate without knowing what counted. And a
    # next_tool added to the server but not here under-counts compliance, which
    # reads as agents ignoring us — so the list is stated, not implied.
    out = summarize_compliance([("trial_preview:generic", 1, 0)])
    assert out["continuation_tools"] == list(CONTINUATION_TOOLS)
    assert "unlock_more_data" in out["continuation_tools"]
