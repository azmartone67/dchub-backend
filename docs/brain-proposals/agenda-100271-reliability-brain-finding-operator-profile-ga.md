<!-- fingerprint:29a1a0fc3e6bf12372a53818b1e378de -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: operator_profile_gap:Equinix, Inc. @ /operators/equinix-inc (seen x223)

> Auto-captured from an **approved** brain agenda item (#100271). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-19T08:40:50.614650Z · agenda #100271_

## The approved recommendation

Add an operator name-alias mapping ('Equinix' -> 'Equinix, Inc.') in the operator resolution layer used by routes/operators.py:537 and apply it before the operator_profile_gap detector evaluates completeness, so both provider strings resolve to /operators/equinix-inc.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/inv-100410-operator-profile-gap-equinix-inc-observed-at.md`, which stays
OPEN as the single obligation for `operator_profile_gap`. This doc's target —
`Equinix, Inc. @ /operators/equinix-inc` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on inv-100410-operator-profile-gap-equinix-inc-observed-at.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is inv-100410-operator-profile-gap-equinix-inc-observed-at.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in inv-100410-operator-profile-gap-equinix-inc-observed-at.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against inv-100410-operator-profile-gap-equinix-inc-observed-at.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of inv-100410-operator-profile-gap-equinix-inc-observed-at.md (class collapse)