# Brain proposal — [reliability] Brain finding: mcp_funnel_concentration_top5 @ /api/v1/mcp/funnel (seen x901)

> Auto-captured from an **approved** brain agenda item (#70). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-09T06:28:27.521347Z · agenda #70_

## The approved recommendation

Approve reclassifying mcp_funnel_concentration_top5 from a per-cycle alert to a stateful, de-looped, baseline-relative detector with finding fingerprinting/rollup — accepting that this will suppress ~900 recurring emissions in exchange for firing only on genuine concentration shifts. Alternatively, direct engineering effort at funnel/load-balancing changes instead (rejected here as the evidence indicates a measurement artifact, not a traffic problem).

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
