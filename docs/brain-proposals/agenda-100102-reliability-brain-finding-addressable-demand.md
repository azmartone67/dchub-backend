# Brain proposal — [reliability] Brain finding: addressable_demand_unconverted @ tool:get_grid_intelligence (seen x190)

> Auto-captured from an **approved** brain agenda item (#100102). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-15T20:16:18.241738Z · agenda #100102_

## The approved recommendation

Approve one of: (A) implement both layers — stateful one-per-condition detector with hysteresis AND wire the existing bind_email/_bind nudge into get_grid_intelligence + get_fiber_intel with landing verification as the resolution criterion; (B) detector redesign only (stops the 190x noise but leaves the conversion gap open); or (C) keep per-instance firing and continue manual triage. Also decide whether the nudge should target all free callers of these two tools or only repeat callers above a usage threshold.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
