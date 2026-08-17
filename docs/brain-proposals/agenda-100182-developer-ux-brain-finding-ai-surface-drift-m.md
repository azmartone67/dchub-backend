<!-- fingerprint:fa8fa1f97b4f41a277e7f6427829a78b -->
**SPEC-ONLY** — this PR changes no running code and is not a fix; it captures an approved recommendation as an implementable spec.

# Brain proposal — [developer_ux] Brain finding: ai_surface_drift:mcp_json:stale_value @ https://dchub.cloud/.well-known/mcp

> Auto-captured from an **approved** brain agenda item (#100182). The brain's
> recommendation couldn't be expressed as a single-file edit, so it's filed here
> as a spec for a human to implement (or discard). **Draft PR — a human merges.**

_Filed 2026-08-09T19:46:33.534272Z · agenda #100182_

## The approved recommendation

Approve building a generate-from-canon pipeline + deploy-time drift gate for all AI surfaces (mcp.json, mcp-server.json, server-card.json, AGENTS.md, /connect), versus continuing per-instance auto-fixes; and decide whether the auto-fixer should be granted write authority to regenerate these files from canon or remain flag-only.

## Triage — 2026-08-17 (class collapse) — CLOSED, class member

Same condition CLASS as `docs/brain-proposals/prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md`, which stays
OPEN as the single obligation for `ai_surface_drift`. This doc's target —
`https://dchub.cloud/.well-known/mcp` — is enumerated in that doc's rolled-up roster, so closing
this copy does not drop the target. Act on prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md.

## Human checklist

- [x] Confirm this is still worth doing — the CLASS is still worth doing — this per-target COPY is not; canonical is prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md
- [x] Scope it to a concrete change (file(s) + approach) — scope belongs to the class in prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md, which enumerates every affected target
- [x] Implement + verify — one fix serves the whole class — implement against prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md
- [x] Or discard this PR if superseded / not worth it — closed 2026-08-17 as a class member of prop-100067-developer-ux-brain-finding-ai-surface-drift-l.md (class collapse)