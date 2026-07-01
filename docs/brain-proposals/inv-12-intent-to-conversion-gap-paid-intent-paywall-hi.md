# Brain proposal — Intent-to-conversion gap: paid-intent paywall hits (mcp_paid_intent) exploded from 7 to 119 per week after the depth-tease gating shipped, but mcp_conversions stayed flat at ~2/week and the last 14 days show 128 claim tokens minted, 11 used, 0 emails captured. Where exactly in the paywall-to-payment chain do these 119 warm signals die, and what fix converts them?

> Auto-captured from an **approved** brain inv item (#12). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-01T23:52:54.781653Z · inv #12_

## The approved recommendation

Choose the fix order: (A) repair the /mcp.json 5xx and ship coaching-rich paywall payloads (price + claim link + value line) that surface the existing claim_/bind_email flow, then re-measure; or (B) first invest in full stage-by-stage funnel instrumentation and distinct-client analysis of the paid_intent spike before changing anything — and decide whether the -96% call collapse is a separate incident to triage first.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
