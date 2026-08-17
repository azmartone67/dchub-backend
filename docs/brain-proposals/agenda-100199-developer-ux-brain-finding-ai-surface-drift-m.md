<!-- fingerprint:7b1b2873b7e85671ef55768926f04879 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: ai_surface_drift:mcp_json:pro_period @ https://dchub.cloud/.well-known/mcp.

> Auto-captured from an **approved** brain agenda item (#100199). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-16T09:50:17.762797Z · agenda #100199_

## The approved recommendation

Approve building a single-source-of-truth pipeline: all AI surfaces (mcp.json, mcp-server.json, server-card.json, AGENTS.md, /connect) rendered from one canonical config with a CI diff-against-canon gate blocking deploys — versus continuing per-field patches via the auto-fixer. Also decide the canonical unit representation for rate limits (per-day vs per-month) so the canon and detector agree before the gate is enforced.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md`, which stays
OPEN as the single obligation for `ai_surface_drift`. This doc's target —
`https://dchub.cloud/.well-known/mcp.` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md (class collapse)