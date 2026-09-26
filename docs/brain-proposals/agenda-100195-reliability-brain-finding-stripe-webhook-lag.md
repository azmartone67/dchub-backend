<!-- fingerprint:79570ffcd1af1506b9d62e8407c345d1 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: stripe_webhook_lag @ table:stripe_webhook_events (value 74)

> Auto-captured from an **approved** brain agenda item (#100195). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-15T07:40:30.886162Z · agenda #100195_

## The approved recommendation

First check Stripe dashboard → Developers → Webhooks: if the endpoint is disabled or failing, fix delivery (path A). If delivery is healthy but events are genuinely sparse, decide between adding a synthetic heartbeat/daily reconciliation pull to make the metric measure pipeline health (path B) or recalibrating the 72h threshold to match real event cadence (path C).

## Rolled-up targets — class `stripe_webhook_lag` (class collapse, 2026-09-26)

This doc is now the single obligation for **2 occurrences** of
`stripe_webhook_lag`. The other 1 were closed against it. They are listed here
in full so the collapse loses no target — fixing the class means fixing
every line below, and a fix that only covers this doc's own target has not
discharged the obligation.

- `table:stripe_webhook_events` — was `agenda-100195-reliability-brain-finding-stripe-webhook-lag.md` (filed 2026-08-15)
- `table:stripe_webhook_events` — was `inv-100213-stripe-webhook-lag-observed-at-table-stripe-we.md` (filed 2026-08-18)

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
