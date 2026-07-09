# Brain proposal — [reliability] Brain finding: mcp_funnel_leak:get_grid_intelligence @ mcp_funnel: tool=get_grid_intellige

> Auto-captured from an **approved** brain agenda item (#73). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-09T06:28:19.701566Z · agenda #73_

## The approved recommendation

Approve (a) refactoring the mcp_funnel_leak detector to fingerprinted, auto-resolving findings (one open finding per tool, closed when the zero-conversion condition clears), and (b) prioritizing execution of the already-recommended orphan grid-page registration + landing-verification — versus continuing to triage each of the 207 instances individually. Also decide whether to bulk-close the existing 207 stale entries after the lifecycle fix lands.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
