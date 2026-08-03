"""
pr-before-after-metric-capture-harness.py — STRATEGIC SCAFFOLD (auto-drafted by Brain L6, week 2026-08-03).

This file is a SCAFFOLD only. The brain wrote it to start the conversation
on a feature it thinks DC Hub should ship next. It does not implement the
feature — a human reviews the spec below and turns this into a real route.

SPEC (from brain L6 synthesis):
The brain cannot trust its own recommendations: 34 of 35 merged PRs are outcome='unknown' with before/after=null, and PR success_rate reads 0.015 purely because outcomes are never recorded. Build a lightweight capture harness that snapshots the target endpoint/metric at PR merge time and re-samples at T+7d, writing a structured outcome row L16 can calibrate against. Without this, every dollar_lift_est in these syntheses is unfalsifiable and the 498-count autopilot recidivist keeps re-firing because we never confirm fixes hold. Success = >80% of merged PRs carry non-null before/after within 30d, and recidivist_count for top labels trends down because shallow patches get caught. This is infrastructure, not glamour, but it is the precondition for every other confidence='high' claim the brain makes.

Evidence cited by the brain when proposing this:
- `pr_outcomes.by_outcome.unknown`
- `self_perception.losses.pr_outcome_verification_blind`
- `recidivist.autopilot.recidivist_count`

To unblock: implement the routes and remove the NotImplementedError below.
"""
from flask import Blueprint, jsonify

strategic_pr_before_after_metric_capture_harness_bp = Blueprint("pr-before-after-metric-capture-harness", __name__)


@strategic_pr_before_after_metric_capture_harness_bp.route("/api/v1/strategic-scaffold/pr-before-after-metric-capture-harness", methods=["GET"])
def _scaffold_health():
    """Health probe so the blueprint can register without 500ing.
    Real endpoint TBD by the human reviewer."""
    return jsonify(
        ok=False,
        scaffold=True,
        message=("This is a strategic scaffold drafted by Brain L6. "
                  "It is not implemented yet."),
        spec_doc="docs/strategic/pr-before-after-metric-capture-harness.md",
    ), 501
