<!-- fingerprint:29a1a0fc3e6bf12372a53818b1e378de -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: operator_profile_gap:Equinix, Inc. @ /operators/equinix-inc (seen x223)

> Auto-captured from an **approved** brain agenda item (#100271). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-19T08:40:50.614650Z · agenda #100271_

## The approved recommendation

Add an operator name-alias mapping ('Equinix' -> 'Equinix, Inc.') in the operator resolution layer used by routes/operators.py:537 and apply it before the operator_profile_gap detector evaluates completeness, so both provider strings resolve to /operators/equinix-inc.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
