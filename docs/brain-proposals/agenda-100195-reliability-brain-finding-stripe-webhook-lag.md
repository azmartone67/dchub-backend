<!-- fingerprint:79570ffcd1af1506b9d62e8407c345d1 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: stripe_webhook_lag @ table:stripe_webhook_events (value 74)

> Auto-captured from an **approved** brain agenda item (#100195). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-15T07:40:30.886162Z · agenda #100195_

## The approved recommendation

First check Stripe dashboard → Developers → Webhooks: if the endpoint is disabled or failing, fix delivery (path A). If delivery is healthy but events are genuinely sparse, decide between adding a synthetic heartbeat/daily reconciliation pull to make the metric measure pipeline health (path B) or recalibrating the 72h threshold to match real event cadence (path C).

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
