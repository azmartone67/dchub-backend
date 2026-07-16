# Brain proposal — [reliability] Brain finding: mcp_funnel_concentration_top5 @ /api/v1/mcp/funnel (seen x783)

> Auto-captured from an **approved** brain agenda item (#100104). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-16T07:04:34.007141Z · agenda #100104_

## The approved recommendation

Choose between: (A) reclassify mcp_funnel_concentration_top5 as an informational/baseline metric with a single stateful open finding and a substance-gated closure (recommended), or (B) treat top-5 concentration as a genuine architectural problem requiring load redistribution work — but only after pulling the actual per-tool share breakdown, which is not yet measured. Also decide the target top-5 share threshold at which the detector should escalate.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
