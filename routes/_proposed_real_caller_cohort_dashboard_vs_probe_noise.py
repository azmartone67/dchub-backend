"""
real-caller-cohort-dashboard-vs-probe-noise.py — STRATEGIC SCAFFOLD (auto-drafted by Brain L6, week 2026-06-29).

This file is a SCAFFOLD only. The brain wrote it to start the conversation
on a feature it thinks DC Hub should ship next. It does not implement the
feature — a human reviews the spec below and turns this into a real route.

SPEC (from brain L6 synthesis):
Headline metrics are dangerously inflated: 43,309 tool calls but only 98 real; 'sweep' shows 93% conversion on 4 sessions while real platforms show 0%. The brain repeatedly mistakes loop/probe traffic for demand, and self-assessment flagged the -96% real-traffic collapse was masked by the addressable_demand_unconverted loop. Build a single internal cohort view that classifies every call as real-external / internal / probe / loop using the existing probe_platforms list, and surfaces a 'real callers 7d/30d' number plus per-tool real-vs-noise ratio. Success = brain layers (L4/L5/L6) consume a real_only metric and stop firing on inflated demand. This is foundational: without it every downstream rec is built on poisoned numbers. Measure by whether real_external_7d trend is tracked and alarmed independently of total request counts.

Evidence cited by the brain when proposing this:
- `funnel.now.real_external_7d`
- `funnel.now.probe_platforms`
- `funnel.now.signals_by_platform_30d`

To unblock: implement the routes and remove the NotImplementedError below.
"""
from flask import Blueprint, jsonify

strategic_real_caller_cohort_dashboard_vs_probe_noise_bp = Blueprint("real-caller-cohort-dashboard-vs-probe-noise", __name__)


@strategic_real_caller_cohort_dashboard_vs_probe_noise_bp.route("/api/v1/strategic-scaffold/real-caller-cohort-dashboard-vs-probe-noise", methods=["GET"])
def _scaffold_health():
    """Health probe so the blueprint can register without 500ing.
    Real endpoint TBD by the human reviewer."""
    return jsonify(
        ok=False,
        scaffold=True,
        message=("This is a strategic scaffold drafted by Brain L6. "
                  "It is not implemented yet."),
        spec_doc="docs/strategic/real-caller-cohort-dashboard-vs-probe-noise.md",
    ), 501
