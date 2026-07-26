<!-- fingerprint:d97bca7955746824b1f3eb9d13820562 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: brain_pattern_acting_but_never_landing @ autopilot_outcomes:inspector_l22_h

> Auto-captured from an **approved** brain agenda item (#100142). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-26T05:50:19.793784Z · agenda #100142_

## The approved recommendation

Approve one of: (a) implement the systemic fix — mandatory effect-based verifier contract in _VERIFIERS plus auto-quarantine of zero-landing patterns after N fires, superseding per-pattern patching; (b) first audit whether the 2026-07-18 fix (brain_findings/9373) is deployed and merely lagging in the worklist; or (c) continue per-instance verifier fixes only. Also decide the quarantine threshold (fires before suppression) and the escalation channel for quarantined patterns.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
