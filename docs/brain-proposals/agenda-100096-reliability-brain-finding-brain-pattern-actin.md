<!-- fingerprint:d97bca7955746824b1f3eb9d13820562 -->
# Brain proposal — [reliability] Brain finding: brain_pattern_acting_but_never_landing @ autopilot_outcomes:inspector_l22_h

> Auto-captured from an **approved** brain agenda item (#100096). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or close). **Draft PR — a human merges.**

_Filed 2026-07-14T07:26:59.271001Z · agenda #100096_

## The approved recommendation

Approve (a) changing the autopilot's success criterion from endpoint 2xx to verified landing, and (b) adding an auto-quarantine circuit-breaker that halts re-firing of any finding-class after N unverified executions and escalates it to you — and choose the value of N (suggested 3-5) — versus continuing with per-pattern verifier fixes only.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or close this PR if superseded / not worth it
