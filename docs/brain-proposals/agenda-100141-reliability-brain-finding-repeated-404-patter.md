<!-- fingerprint:1e9800bb3527d65010c4ba477612cc8c -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: repeated_404_pattern @ /api/v1/energy/naturalgas/price (seen x171)

> Auto-captured from an **approved** brain agenda item (#100141). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-26T05:50:23.432477Z · agenda #100141_

## The approved recommendation

Choose the systemic remedy: (A) fund the route-contract-in-CI fence + deprecation policy (stops the whole 404 finding class), (B) first triage whether /api/jobs/gas-refresh being dead is the root of the energy cluster and revive/remove that subsystem deliberately, or (C) continue per-route alias patches as before (fastest, but the pattern will recur). A curl of https://dchub.cloud/api/v1/energy/naturalgas/price plus a check of who the callers are should precede final commitment.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-100138-reliability-brain-finding-repeated-404-patter.md`, which stays
OPEN as the single obligation for `repeated_404_pattern`. This doc's target —
`/api/v1/energy/naturalgas/price` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-100138-reliability-brain-finding-repeated-404-patter.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-100138-reliability-brain-finding-repeated-404-patter.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-100138-reliability-brain-finding-repeated-404-patter.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-100138-reliability-brain-finding-repeated-404-patter.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-100138-reliability-brain-finding-repeated-404-patter.md (class collapse)