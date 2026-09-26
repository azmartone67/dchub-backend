<!-- fingerprint:781354276b75796b3d4ef7976831b4f5 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: ai_surface_drift:agents_md:developer_period @ https://dchub.cloud/AGENTS.md

> Auto-captured from an **approved** brain agenda item (#100246). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-09-03T21:34:50.922946Z · agenda #100246_

## The approved recommendation

In ai_surface_sentinel.py, trace where AGENTS.md, .well-known/mcp.json, and server-card.json get their tier period fields (developer_period/starter_period/pro_period) and refactor all three to render those values from the single canonical pricing map that funnel_health uses for MRR, then open one dchub-backend PR that adds a publish-time regeneration step so the three surfaces cannot drift field-by-field.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md`, which stays
OPEN as the single obligation for `ai_surface_drift`. This doc's target —
`agents_md:developer_period @ https://dchub.cloud/AGENTS.md` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md (class collapse)