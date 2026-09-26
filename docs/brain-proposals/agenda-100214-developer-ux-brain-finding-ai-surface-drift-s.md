<!-- fingerprint:de5eada6c86165bdc88cdc7a64f53738 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: ai_surface_drift:server_card:starter_period @ https://dchub.cloud/.well-kno

> Auto-captured from an **approved** brain agenda item (#100214). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-21T23:34:13.917489Z · agenda #100214_

## The approved recommendation

Choose (a) the canonical unit for tier limits (calls/day vs calls/month) and whether the canon or the live surfaces are the source of truth for the current *_period values, and (b) approve building single-source generation of the four AI surfaces from that canon plus unit-aware comparison in ai_surface_sentinel — versus the cheaper option of only adding unit normalization to the comparator and continuing manual surface edits.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md`, which stays
OPEN as the single obligation for `ai_surface_drift`. This doc's target —
`server_card:starter_period @ https://dchub.cloud/.well-kno` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md (class collapse)