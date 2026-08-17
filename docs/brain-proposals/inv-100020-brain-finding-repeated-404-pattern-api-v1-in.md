<!-- fingerprint:e109f9b34f91f7c3309b51d4c8111723 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — Brain finding: repeated_404_pattern @ /api/v1/infrastructure/transmission returned 404 213 times in the last 24h. What is the root cause, and what is the single highest-leverage fix?

> Auto-captured from an **approved** brain inv item (#100020). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-28T07:21:35.096252Z · inv #100020_

## The approved recommendation

Choose: (a) run the curl probe to confirm edge-vs-origin 404, then ship one edge-level structured-404/API-catalog response covering the whole hallucinated-endpoint family (recommended, fixes 6 known patterns at once); (b) implement /api/v1/infrastructure/transmission as a real endpoint if a legitimate consumer needs transmission data; or (c) treat the 217-count as a detector artifact and open a bug against the brain detector's log source instead.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-100138-reliability-brain-finding-repeated-404-patter.md`, which stays
OPEN as the single obligation for `repeated_404_pattern`. This doc's target —
`/api/v1/infrastructure/transmission` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-100138-reliability-brain-finding-repeated-404-patter.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-100138-reliability-brain-finding-repeated-404-patter.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-100138-reliability-brain-finding-repeated-404-patter.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-100138-reliability-brain-finding-repeated-404-patter.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-100138-reliability-brain-finding-repeated-404-patter.md (class collapse)