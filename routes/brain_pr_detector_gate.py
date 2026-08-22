"""Claim Loop step 5 integration point for the detector-with-the-fix rule.

The public claims feed (OPS_CLAIMS_DETECTOR_MODULE, default this module)
lazily imports `brain_pr_carries_detector` from here. The rule itself lives
in util/brain_detector_rule.py — pure, no Flask, no DB — so the radar tests
and the weekly number evaluate one implementation. This module only
re-exports it; keep it that way.
"""
from util.brain_detector_rule import (  # noqa: F401
    RULE_NAME,
    brain_pr_carries_detector,
    evaluate_pr,
    evaluate_pr_remote,
)

__all__ = ["RULE_NAME", "brain_pr_carries_detector", "evaluate_pr", "evaluate_pr_remote"]
