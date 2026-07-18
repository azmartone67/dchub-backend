<!-- fingerprint:e6f79fee9a3b8cbd21db733dfc61a56a -->
# Brain proposal — [reliability] Brain finding: brain_pattern_acting_but_never_landing @ autopilot_outcomes:outbound_distri

> Auto-captured from an **approved** brain agenda item (#100112). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-18T07:40:45.942731Z · agenda #100112_

## The approved recommendation

Approve the structural change: adopt effect-verified success semantics plus an auto-quarantine circuit-breaker across all autopilot patterns (and immediately quarantine outbound_distribution_health pending its verifier rewrite) — or reject in favor of continuing per-pattern verifier fixes, accepting that the finding will likely keep recurring.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
