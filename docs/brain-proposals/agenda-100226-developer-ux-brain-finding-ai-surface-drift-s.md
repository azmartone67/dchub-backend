<!-- fingerprint:098ee3b5f49c2d6f4e0303682b0ad1c6 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: ai_surface_drift:server_card:pro_period @ https://dchub.cloud/.well-known/m

> Auto-captured from an **approved** brain agenda item (#100226). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-25T21:48:16.549374Z · agenda #100226_

## The approved recommendation

Choose the systemic remedy: (a) extend the PR #3158 canon-generation pipeline so all four AI surfaces render tier/quota fields (and version) from canon at deploy, with a sentinel-gated deploy that blocks on any ai_surface_drift; or (b) keep detect-and-patch via the auto-fixer. Also decide the truth direction first: are the live daily caps (e.g. 2,000 calls/day) or the canon monthly quotas (e.g. 60,000 calls/month) the intended policy?

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md`, which stays
OPEN as the single obligation for `ai_surface_drift`. This doc's target —
`server_card:pro_period @ https://dchub.cloud/.well-known/m` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md (class collapse)