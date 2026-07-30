<!-- fingerprint:4182dae712e8b366c2ce460c03b9fafc -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: trial_to_paid_stagnation @ funnel:signals_to_conversions (seen x6584)

> Auto-captured from an **approved** brain agenda item (#100150). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-30T02:54:59.356665Z · agenda #100150_

## The approved recommendation

Approve converting trial_to_paid_stagnation to a stateful, deduplicated finding with a recalibrated baseline (real-invoice payers over distinct external callers, weekly cadence) — versus keeping the current per-cycle re-fire behavior and continuing to triage instances individually. Also decide whether to bundle this with the previously-recommended landing-verification fix so resolved states actually close.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
