<!-- fingerprint:1320294e8e78535841d5f33210a709e9 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — agent-eval: mistral's #1 structural ask

> Auto-captured from an **approved** brain agenda item (#100119). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-07-20T10:04:52.018342Z · agenda #100119_

## The approved recommendation

Approve building a single spatial set-query capability (query_parcels: GeoJSON polygon + capacity/ISO filters, exposed as an MCP tool reusing the analyze_parcel geometry engine), versus the cheaper alternative of adding polygon filter params to /refined — and confirm whether parcel-level capacity_mw data exists or must be sourced before committing scope.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/agenda-100116-agent-eval-cohere-s-1-structural-ask.md`, which stays
OPEN as the single obligation for `structural_ask`. This doc's target —
`mistral` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on agenda-100116-agent-eval-cohere-s-1-structural-ask.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is agenda-100116-agent-eval-cohere-s-1-structural-ask.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in agenda-100116-agent-eval-cohere-s-1-structural-ask.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against agenda-100116-agent-eval-cohere-s-1-structural-ask.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of agenda-100116-agent-eval-cohere-s-1-structural-ask.md (class collapse)