"""
conversion-attribution-pipeline-repair.py — STRATEGIC SCAFFOLD (auto-drafted by Brain L6, week 2026-07-06).

This file is a SCAFFOLD only. The brain wrote it to start the conversation
on a feature it thinks DC Hub should ship next. It does not implement the
feature — a human reviews the spec below and turns this into a real route.

SPEC (from brain L6 synthesis):
All 6 conversions in the last 30 days landed as 'unattributed' — conversions_attributed_30d=0 while conversions_unattributed_30d=6. This means every funnel optimization the brain proposes is unverifiable: we cannot tell which platform, tool, or claim produced revenue, which is exactly why the strategic-outcome ledger shows 0 verified recs. Build: a lightweight attribution join that stamps every Stripe/conversion event with the originating api_key → platform → last-N tool calls → claim_id chain, backfilled from existing signals_by_platform and claim tables. Success looks like: ≥80% of conversions in the next 30d carry a platform and tool attribution, exposed at /api/v1/funnel/attribution and consumed by L6 for future dollar estimates. This is the prerequisite gap — without it, PR #1402's claim-activation work and every future funnel rec stays 'unknown' forever. Ship as a read-side join first (no billing-path changes), then wire the write-side stamp.

Evidence cited by the brain when proposing this:
- `funnel.now.conversions_unattributed_30d`
- `funnel.now.conversions_attributed_30d`
- `self_perception.latest.losses`

To unblock: implement the routes and remove the NotImplementedError below.
"""
from flask import Blueprint, jsonify

strategic_conversion_attribution_pipeline_repair_bp = Blueprint("conversion-attribution-pipeline-repair", __name__)


@strategic_conversion_attribution_pipeline_repair_bp.route("/api/v1/strategic-scaffold/conversion-attribution-pipeline-repair", methods=["GET"])
def _scaffold_health():
    """Health probe so the blueprint can register without 500ing.
    Real endpoint TBD by the human reviewer."""
    return jsonify(
        ok=False,
        scaffold=True,
        message=("This is a strategic scaffold drafted by Brain L6. "
                  "It is not implemented yet."),
        spec_doc="docs/strategic/conversion-attribution-pipeline-repair.md",
    ), 501
