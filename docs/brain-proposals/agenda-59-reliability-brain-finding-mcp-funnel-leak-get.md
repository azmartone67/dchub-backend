# Brain proposal — [reliability] Brain finding: mcp_funnel_leak:get_grid_intelligence @ mcp_funnel: tool=get_grid_intellige

> Auto-captured from an **approved** brain agenda item (#59). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-05T21:07:40.229784Z · agenda #59_

## The approved recommendation

Choose between (A) authorizing a framework-level refactor of mcp_funnel's shared resource/lifecycle handling (higher effort, stops recurrence for all tools including get_market_intel) vs (B) continuing per-tool patches on get_grid_intelligence only; and separately, approve changing the leak detector to aggregate repeated identical signatures into one escalating finding instead of 203 worklist entries.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
