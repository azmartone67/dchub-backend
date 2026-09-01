"""context_integrity lane 2 — is a lesson about the platform, or about a blind probe?

WHY THIS EXISTS
---------------
Measured against all 20 active L18 lessons on 2026-08-31, the classifier scored
11/20 (55%) and failed its 50% threshold. Hand-checking all 20 found it was
scoring 11 by way of two defects pointing in opposite directions:

    4 false positives   `_NULL_VERDICT` ran against the whole lesson, so
                        "absent" counted even where it named what the
                        instrument SAW. "endpoint health verified true when
                        endpoint returns ok, populated queue data, and specific
                        finding is absent" is a successful reading.
    4 false negatives   `payload` had no `s?` while `endpoint` was spelled
                        twice to get one, so every "...without segmented demand
                        payloads" lesson scored clean.

They cancelled. The lane's verdict was right by accident, and a blindness
detector that is right by accident is precisely what this shell exists to name.

★ THE VERDICT DOES NOT CHANGE. It is 11/20 before and after, and the lane stays
RED — 55% of post-fix lessons really are about instrument blindness. These tests
pin that, so nobody later reads this fix as an attempt to green the lane.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from routes.context_integrity_master_shell import (  # noqa: E402
    _BLINDNESS_FAIL_SHARE, _is_instrument_blindness)


# ── the four that were wrongly counted as blindness ──────────────────
# Verbatim from /api/v1/brain/lessons on 2026-08-31.

WORKED = [
    ("Action-queue endpoint health verified true when endpoint returns ok "
     "status, populated queue data, and specific finding is absent—all three "
     "conditions required for resolution confirmation."),
    ("Action-queue endpoint health verifies true when endpoint returns ok "
     "status, populated queue data, AND specific finding is absent from "
     "visible queue—all three conditions required simultaneously."),
    ("QA-resolve predictions for fast_qa findings falsify when endpoint "
     "returns ok status and queue shows other active findings but specific "
     "detector:domain pair absent—indicates reclassification, aging, or "
     "priority shift, not operational fix."),
    ("QA-resolve predictions falsify when endpoint returns ok and "
     "action-queue shows other findings active but specific detector:domain "
     "pair absent—indicates reclassification or aging, not fix."),
]


@pytest.mark.parametrize("lesson", WORKED)
def test_a_lesson_whose_instrument_returned_a_reading_is_not_blindness(lesson):
    """★ The instrument answered — ok status, populated queue — and the thing
    it reports absent is the FINDING, which clearing is the good outcome. The
    two `falsify` lessons are real domain knowledge: a finding vanishing means
    reclassification, not a fix. Counting these as blindness inflates the very
    number the lane gates on."""
    assert _is_instrument_blindness(lesson) is False


# ── the four that were wrongly counted as clean ──────────────────────

PAYLOADS = [
    ("Annualized run-rate and cumulative demand predictions null when "
     "annualized_run_rate_from_7d and headline_calls not both retrieved—cannot "
     "verify basis alignment without dual-counter comparison payloads."),
    ("Internal-traffic inflation predictions null when "
     "ai_agent_requests_external, real_external_calls_7d, or internal/external "
     "series breakdowns not queried—cannot verify 40x gap or repointing "
     "without segmented demand payloads."),
    ("Unattributed-cohort and caller-classification predictions null when "
     "calls_by_platform_30d, top_platforms breakdown, or IP-to-platform "
     "mapping not retrieved—cannot verify redistribution or classification "
     "without segmented demand payloads."),
    ("Internal-traffic inflation predictions null when "
     "ai_agent_requests_external, real_external_calls_7d, or internal/external "
     "series breakdowns not queried—cannot verify segmentation or repointing "
     "without traffic-source payloads."),
]


@pytest.mark.parametrize("lesson", PAYLOADS)
def test_the_plural_payloads_lessons_are_counted(lesson):
    """`payload` carried no `s?` while `endpoint` was written twice to get one.
    Every one of these states a null verdict and names the payload it lacked."""
    assert _is_instrument_blindness(lesson) is True


def test_singular_and_plural_are_symmetric():
    """The asymmetry was the whole bug — pin both forms of both nouns."""
    for noun in ("endpoint", "endpoints", "payload", "payloads"):
        assert _is_instrument_blindness(f"predictions null—no {noun} data"), noun


# ── genuine blindness still counts ───────────────────────────────────

STILL_BLIND = [
    ("Predictions verify, falsify, or null based on data availability—empty "
     "endpoints or missing resolved_evidence always yield null outcomes, never "
     "false positives from absence alone."),
    ("Predictions requiring multi-endpoint payloads (concentration_flag + "
     "calls_net_of_top) null when any required endpoint returns empty—verify "
     "all legs queried before outcome."),
    ("Identity-surface predictions null when both /dchub and /mcp endpoints "
     "return empty—dual-surface query pattern required but insufficient if "
     "neither surface populates data."),
    ("Endpoint-specific infrastructure predictions null when target endpoint "
     "returns empty regardless of domain—/fetcher, /signup, /daily all show "
     "pattern of empty response blocking verification."),
]


@pytest.mark.parametrize("lesson", STILL_BLIND)
def test_real_blindness_survives_the_narrowing(lesson):
    """★ Three of these contain the word "verify" or "falsify" — as an
    imperative or in a list of possible outcomes, not as the verdict. A crude
    "mentions verify => not blindness" rule would drop them, which is why the
    verdict clause is split off rather than substring-matched."""
    assert _is_instrument_blindness(lesson) is True


def test_a_domain_lesson_is_never_swept_in():
    """The docstring's own example, and the reason the conjunction exists."""
    assert not _is_instrument_blindness(
        "spike-decay predictions falsify when the post-spike week collapses")


def test_a_long_observation_window_is_a_constraint_not_blindness():
    """Live lesson. Nulls because 30 days have not elapsed — the instrument is
    fine, the clock is slow. No endpoint/payload subject, so it must not count."""
    assert not _is_instrument_blindness(
        "Predictions awaiting 30-day reconciliation metrics "
        "(signal_bridged_30d) consistently null—long observation windows "
        "prevent rapid verification cycles.")


# ── the verdict is unchanged ─────────────────────────────────────────

def test_the_live_corpus_still_fails_the_lane():
    """★ THE ANCHOR. All 20 active lessons, verbatim. 11 were counted before
    this fix and 11 after — the errors cancelled. The share stays 55% against a
    50% threshold and the lane stays RED, because 55% of what the brain learned
    since the envelope shipped really is about its own blind probes.

    If a later change makes this pass, it changed the verdict, not the bug."""
    corpus = WORKED + PAYLOADS + STILL_BLIND + [
        ("Agent-identity and distribution predictions verify when endpoint "
         "explicitly documents classification methodology AND shows current "
         "agent_id distribution—methodology explanation plus current state "
         "both required, not historical alone."),
        ("Registry-coverage predictions verify true when coverage endpoint "
         "shows improved enumeration ratio AND dormant-bot list returns "
         "empty—both discovery improvement and consequence clearance required "
         "together."),
        ("Registry-coverage predictions verify when coverage ratio improves "
         "AND dormant-bot list returns empty—both discovery improvement and "
         "consequence clearance required together."),
        ("Predictions awaiting 30-day reconciliation metrics "
         "(signal_bridged_30d, conversions_channel_fallback_30d, "
         "calls_by_platform_30d redistribution) consistently null—long "
         "observation windows prevent rapid verification cycles."),
        ("Attribution-bridge predictions null when "
         "conversions_reconciliation_30d, signal_bridged_30d, "
         "conversions_channel_fallback_30d not retrieved—30-day observation "
         "windows and both attribution legs required simultaneously."),
        ("Predictions requiring multi-leg reconciliation metrics "
         "(calls_by_platform_30d, signal_bridged_30d) consistently null when "
         "endpoints return empty—verify endpoint implementation and data "
         "population before declaring outcome."),
        ("Cross-surface divergence predictions null when "
         "cross_surface_metric_divergence endpoint absent—cannot verify "
         "hardcoded-vs-canonical alignment without comparison payloads showing "
         "detector clearance."),
        ("Definition-bump predictions null when measurement_definition_changed "
         "detector status and changelog marker verification endpoints both "
         "absent—cannot verify process adoption without dual confirmation."),
    ]
    assert len(corpus) == 20, "the live corpus was 20 active lessons"
    blind = [c for c in corpus if _is_instrument_blindness(c)]
    assert len(blind) == 11, f"expected 11 as measured live, got {len(blind)}"
    assert len(blind) / 20.0 >= _BLINDNESS_FAIL_SHARE, \
        "the lane must still be RED — this fix corrects the count, not the verdict"


def test_the_threshold_was_not_moved():
    """The cheap way to green a lane is to raise its bar. Pin it."""
    assert _BLINDNESS_FAIL_SHARE == 0.50
