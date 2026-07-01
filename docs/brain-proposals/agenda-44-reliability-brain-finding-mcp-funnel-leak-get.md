# Brain proposal — [reliability] Brain finding: mcp_funnel_leak:get_interconnection_queue @ mcp_funnel: tool=get_interconne

> Auto-captured from an **approved** brain agenda item (#44). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-01T20:40:11.559050Z · agenda #44_

## The approved recommendation

Approve investigating and fixing the shared mcp_funnel response/lifecycle handler (covering both get_interconnection_queue and get_market_intel) plus adding a regression fence to HEALTH_BASELINE fences, versus the human's alternative of continuing per-instance patching — and pull the mcp_funnel handler code + occurrence log needed to pin the exact leak mechanism before coding.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
