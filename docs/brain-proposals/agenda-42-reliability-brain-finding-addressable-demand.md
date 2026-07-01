# Brain proposal — [reliability] Brain finding: addressable_demand_unconverted @ tool:get_fiber_intel (seen x189)

> Auto-captured from an **approved** brain agenda item (#42). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-01T08:30:13.173490Z · agenda #42_

## The approved recommendation

Decide whether to (a) inspect get_fiber_intel's emission logic and confirm whether a conversion-status write-back already exists before building anything, and (b) approve wiring detected fiber demand into a closed-loop conversion sink with a status field + de-dup — versus continuing to acknowledge each of the 189 instances individually.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
