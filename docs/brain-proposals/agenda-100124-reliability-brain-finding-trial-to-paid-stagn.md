<!-- fingerprint:4182dae712e8b366c2ce460c03b9fafc -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: trial_to_paid_stagnation @ funnel:signals_to_conversions (seen x4944)

> Auto-captured from an **approved** brain agenda item (#100124). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-21T18:11:01.190551Z · agenda #100124_

## The approved recommendation

Approve (a) refactoring trial_to_paid_stagnation into a stateful, deduped detector with a probe-excluded, invoice-verified denominator, and (b) wiring the addressable_demand_unconverted signal to the already-live bind_email/_bind nudge path — or decide instead to keep the current per-fire escalation model and simply rate-limit the detector.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
