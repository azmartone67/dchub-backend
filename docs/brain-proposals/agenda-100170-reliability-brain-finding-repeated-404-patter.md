<!-- fingerprint:d2c03c40e632c48bf15e8c7741e9a89a -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: repeated_404_pattern @ /js/dchub-nav.js (seen x196)

> Auto-captured from an **approved** brain agenda item (#100170). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-05T05:17:04.548220Z · agenda #100170_

## The approved recommendation

Choose the fix direction: (a) restore /js/dchub-nav.js to the build (if the nav script is still needed), or (b) remove/replace the stale reference and serve a 410 for the path — and approve adding the CI asset-manifest check as the class-level prevention, versus continuing per-instance patching.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-100138-reliability-brain-finding-repeated-404-patter.md`, which stays
OPEN as the single obligation for `repeated_404_pattern`. This doc's target —
`/js/dchub-nav.js` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-100138-reliability-brain-finding-repeated-404-patter.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-100138-reliability-brain-finding-repeated-404-patter.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-100138-reliability-brain-finding-repeated-404-patter.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-100138-reliability-brain-finding-repeated-404-patter.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-100138-reliability-brain-finding-repeated-404-patter.md (class collapse)