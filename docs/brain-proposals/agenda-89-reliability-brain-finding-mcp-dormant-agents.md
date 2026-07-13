# Brain proposal — [reliability] Brain finding: mcp_dormant_agents_present @ /api/v1/bots/dormant (seen x50)

> Auto-captured from an **approved** brain agenda item (#89). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-13T05:25:40.063980Z · agenda #89_

## The approved recommendation

Choose the fix order: (A) implement stateful finding lifecycle first (cheap, stops the alert noise immediately but doesn't reduce real churn), (B) implement early-decay identity/nudge loop first (addresses root-cause churn using already-live bind capabilities but needs tuning), or (C) do both in one change. Also decide the early-decay trigger threshold (e.g., days-idle before nudge) since dormancy timing data was not measured.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
