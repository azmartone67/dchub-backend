<!-- fingerprint:08296abe15238a2ac30209fa60899b04 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [reliability] Brain finding: repeated_404_pattern @ /api/v1/energy/retail/rates (seen x164)

> Auto-captured from an **approved** brain agenda item (#100143). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-26T05:50:12.990310Z · agenda #100143_

## The approved recommendation

Choose the direction for the /api/v1/energy/* and /api/grid/* 404 family: (a) implement the endpoints backed by existing grid/energy data (possibly sourcing retail rates), (b) formally decommission them with explicit 410s and fix/notify the callers, or (c) do the caller-origin audit first and defer the build/kill call — and approve adding a route-contract CI check either way.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-100138-reliability-brain-finding-repeated-404-patter.md`, which stays
OPEN as the single obligation for `repeated_404_pattern`. This doc's target —
`/api/v1/energy/retail/rates` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-100138-reliability-brain-finding-repeated-404-patter.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-100138-reliability-brain-finding-repeated-404-patter.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-100138-reliability-brain-finding-repeated-404-patter.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-100138-reliability-brain-finding-repeated-404-patter.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-100138-reliability-brain-finding-repeated-404-patter.md (class collapse)