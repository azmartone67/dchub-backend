<!-- fingerprint:377284d9538d0336025d8a8800a509c5 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: ai_surface_drift:mcp_json:starter_period @ https://dchub.cloud/.well-known/

> Auto-captured from an **approved** brain agenda item (#100205). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-18T03:00:45.398296Z · agenda #100205_

## The approved recommendation

Approve building a single canonical AI-surface manifest (structured plan limits with units, version, tool count) with deploy-time generation of mcp.json / mcp-server.json / server-card.json / AGENTS.md blocks / connect, plus a CI drift gate — versus continuing to let the auto-fixer patch each drift finding per-instance. If approved, decide whether AGENTS.md gets full generation or templated-block linting.

## Triage — 2026-09-26 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md`, which stays
OPEN as the single obligation for `ai_surface_drift`. This doc's target —
`mcp_json:starter_period @ https://dchub.cloud/.well-known/` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-09-26 as a class member of prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md (class collapse)