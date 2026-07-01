# Brain proposal — [reliability] Brain finding: mcp_funnel_concentration_top5 @ /api/v1/mcp/funnel (seen x869)

> Auto-captured from an **approved** brain agenda item (#45). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-01T20:40:02.285255Z · agenda #45_

## The approved recommendation

Decide whether to (A) add a minimum-volume floor to the mcp_funnel_concentration_top5 detector so it suppresses when external call counts are statistically meaningless, and fold the finding into the activation/retention workstream — or (B) keep it firing as-is and prioritize investigating actual top-5 tool distribution first before adjusting the detector.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
